"""负载基准 · SSE 多客户端（AUDIT_REVIEW §10 P3「无负载基准」）。

    .venv\\Scripts\\python.exe tests\\bench_load_sse.py [--clients 1,10,39,60,100] [--events 200]
        [--rate 200] [--probes 20] [--json]

在随机空闲端口起真实 uvicorn（lifespan=off，不跑 startup，不碰任何数据目录；
配置路径仍整体指向临时目录兜底），对每个并发档位 N：

1. **建连**：N 个客户端同时连 /api/events，记录「发起 → 收到 history-end」耗时；
2. **扇出**：从服务端进程内 evolve.bus.publish() 以固定速率发 M 条事件，每条带
   perf_counter 时间戳，客户端记录送达延迟；同时核对每个客户端是否收全 M 条；
3. **饿死探针**：N 个 SSE 连接保持空闲时，串行请求一个纯内存的同步接口 /api/perf
   （不碰库）K 次，记录延迟——它与 SSE 生成器共用 AnyIO 默认线程池（40 个令牌）；
4. **内存**：tracemalloc 记录本档位整个过程的 Python 峰值（服务端与客户端同进程，
   客户端线程开销也计入，只用于档位间相对比较）。
"""
from __future__ import annotations

import argparse
import gc
import json
import socket
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from backend import config  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def pct(samples: list[float], q: float) -> float:
    s = sorted(samples)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def summarize_ms(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    return {"n": len(samples), "p50_ms": round(statistics.median(samples) * 1000, 1),
            "p95_ms": round(pct(samples, .95) * 1000, 1), "max_ms": round(max(samples) * 1000, 1)}


class Client(threading.Thread):
    """一个 SSE 订阅者：连上后等 history-end，之后收 seq 事件直到收满或被叫停。"""

    def __init__(self, url: str, expect: int, stop: threading.Event) -> None:
        super().__init__(daemon=True)
        self.url, self.expect, self.stop = url, expect, stop
        self.ready = threading.Event()
        self.connect_s: float | None = None
        self.lat: list[float] = []
        self.got = 0
        self.error = ""

    def run(self) -> None:
        t0 = time.perf_counter()
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), trust_env=False) as c:
                with c.stream("GET", self.url) as r:
                    event = ""
                    for line in r.iter_lines():
                        if self.stop.is_set():
                            return
                        if line.startswith("event:"):
                            event = line[6:].strip()
                            continue
                        if line.startswith("data:"):
                            if event == "history-end":
                                self.connect_s = time.perf_counter() - t0
                                self.ready.set()
                            else:
                                try:
                                    evt = json.loads(line[5:])
                                except ValueError:
                                    evt = {}
                                if "bench_t" in evt:
                                    self.lat.append(time.perf_counter() - evt["bench_t"])
                                    self.got += 1
                                    if self.got >= self.expect:
                                        return
                            event = ""
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"[:120]
        finally:
            self.ready.set()


def run_level(base: str, n: int, m: int, rate: float, probes: int) -> dict:
    from backend import evolve

    gc.collect()
    tracemalloc.start()
    stop = threading.Event()
    clients = [Client(f"{base}/api/events", m, stop) for _ in range(n)]
    for c in clients:
        c.start()
    deadline = time.time() + 60
    for c in clients:
        c.ready.wait(max(0.0, deadline - time.time()))
    connected = [c for c in clients if c.connect_s is not None]

    # 饿死探针：SSE 全部空闲时请求同步接口
    probe_lat, probe_err = [], 0
    with httpx.Client(timeout=30.0, trust_env=False) as pc:
        for _ in range(probes):
            t0 = time.perf_counter()
            try:
                pc.get(f"{base}/api/perf").raise_for_status()
                probe_lat.append(time.perf_counter() - t0)
            except Exception:  # noqa: BLE001
                probe_err += 1

    # 扇出：固定速率发布
    gap = 1.0 / rate if rate > 0 else 0.0
    pub_cost = []
    t_start = time.perf_counter()
    for i in range(m):
        target = t_start + i * gap
        while time.perf_counter() < target:
            time.sleep(min(.001, max(0.0, target - time.perf_counter())))
        t0 = time.perf_counter()
        evolve.bus.publish({"level": "info", "msg": f"bench {i}", "bench_seq": i, "bench_t": t0})
        pub_cost.append(time.perf_counter() - t0)
    for c in clients:
        c.join(timeout=max(10.0, 6.0 + m / max(rate, 1)))
    stop.set()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    lat = [x for c in connected for x in c.lat]
    complete = sum(1 for c in clients if c.got >= m)
    errors = [c.error for c in clients if c.error]
    return {
        "clients": n, "events": m, "rate_per_s": rate,
        "connected": len(connected),
        "connect": summarize_ms([c.connect_s for c in connected]),
        "delivery": summarize_ms(lat),
        "complete_clients": complete,
        "publish_call": summarize_ms(pub_cost),
        "probe_sync_endpoint": summarize_ms(probe_lat) | {"errors": probe_err},
        "peak_py_mb": round(peak / 1e6, 1),
        "errors": errors[:3],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", default="1,10,39,60,100")
    ap.add_argument("--events", type=int, default=200)
    ap.add_argument("--rate", type=float, default=200.0)
    ap.add_argument("--probes", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    levels = [int(x) for x in args.clients.split(",") if x.strip()]

    import uvicorn

    tmp = Path(tempfile.mkdtemp(prefix="evomind-bench-sse-"))
    results = []
    with patch.multiple(config, DATA=tmp, LOGS=tmp / "logs", MODELS=tmp / "models",
                        DATASETS=tmp / "datasets", KNOWLEDGE=tmp / "knowledge",
                        CONFIG_FILE=tmp / "config.json", DB_FILE=tmp / "knowledge" / "knowledge.db",
                        _cache=None):
        from backend import evolve, main as app_main

        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(
            app_main.app, host="127.0.0.1", port=port, lifespan="off",
            log_level="error", timeout_graceful_shutdown=2))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(.05)
        base = f"http://127.0.0.1:{port}"
        try:
            for n in levels:
                evolve.bus.history.clear()
                results.append(run_level(base, n, args.events, args.rate, args.probes))
                # 让上一档的生成器在下一次 ping（≤5s）时发现断线、退订
                for _ in range(80):
                    with evolve.bus._lock:
                        left = len(evolve.bus._subs)
                    if left == 0:
                        break
                    time.sleep(.1)
        finally:
            server.should_exit = True
            th.join(timeout=10)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
        return
    print(f"SSE 负载：每档 {args.events} 条事件 @ {args.rate:.0f}/s，同步探针 {args.probes} 次")
    print(f"{'N':>4} {'建连p50':>8} {'建连max':>8} {'送达p50':>8} {'送达p95':>8} {'送达max':>8} "
          f"{'收全':>6} {'探针p50':>8} {'探针p95':>8} {'探针max':>8} {'峰值MB':>7}")
    for r in results:
        c, d, p = r["connect"], r["delivery"], r["probe_sync_endpoint"]
        print(f"{r['clients']:>4} {c.get('p50_ms', '-'):>8} {c.get('max_ms', '-'):>8} "
              f"{d.get('p50_ms', '-'):>8} {d.get('p95_ms', '-'):>8} {d.get('max_ms', '-'):>8} "
              f"{r['complete_clients']:>3}/{r['clients']:<3}{p.get('p50_ms', '-'):>8} "
              f"{p.get('p95_ms', '-'):>8} {p.get('max_ms', '-'):>8} {r['peak_py_mb']:>7}"
              + (f"  错误样例：{r['errors'][0]}" if r["errors"] else ""))


if __name__ == "__main__":
    main()
