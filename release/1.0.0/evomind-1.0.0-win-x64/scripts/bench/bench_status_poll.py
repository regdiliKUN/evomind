"""A/B：/api/status 轮询组成开销。

改动前，仪表盘每几秒轮询一次的 /api/status 每次都要：
  1. llm.healthy()      → 一次 HTTP GET {ollama}/api/tags
  2. llm.list_models()  → 又一次 HTTP GET {ollama}/api/tags（同一个端点，纯重复往返）
  3. memory.stats()     → 内部 config.disk_usage_gb()：对 data/models + data/datasets
                          整棵 rglob 走一遍 + 每个文件 stat 一次
  4. downloader.disk_report() → 内部又一次 config.disk_usage_gb()：第二次全量 rglob
  5. finetune.check_env() → import torch + torch.cuda.is_available()（驱动调用）

改动后：llm.tags() 一次往返拿回 (healthy, models) 并加 2s TTL；disk_usage_gb 加 5s TTL；
check_env 加 30s TTL。

本基准起一个本地 stub Ollama（/api/tags，带真实结构的模型清单），24 轮交替跑
「旧组成」与「新组成（直接调生产代码 main.api_status）」，核验：
  - 两种组成返回的 payload 完全一致（等价性）
  - stub 收到的 HTTP 请求数：旧 ≈ 2×调用数，新 ≈ 1×调用数（TTL 内合并）
  - 24/24 轮新组成更快

旧组成按「改动前的 api_status」逐字复刻：healthy/list_models 各调一次、disk 部分调
config._disk_usage_gb_uncached() 两次、check_env 用 finetune._check_env_uncached()。
其余字段（engine/stats/config/disk_report/finetune_state）与生产代码走同一批函数，
这些函数本身未被本次改动影响（disk_usage_gb 的 TTL 只会让旧路径更快，即基准低估增益）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()

# 隔离后把 MODELS/DATASETS 也指到基准目录（config 模块级常量在 import 时按真实
# DATA 算好了，不重新指派的话 rglob 会去扫用户真实的模型目录）
from backend import config  # noqa: E402

config.MODELS = config.DATA / "models"
config.DATASETS = config.DATA / "datasets"

_env.fake_llm()

from backend import downloader, evolve, finetune as ft, llm, memory  # noqa: E402
from backend import main  # noqa: E402

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
#: 相邻两次「轮询」之间的间隔（秒）。0 = 连续突发（反复切换视图/刷新），
#: 6 = 仪表盘真实的 setInterval(refreshAll, 6000) 节奏。
GAP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0


# --------------------------------------------------------------------------
# stub Ollama：只实现 /api/tags，统计收到的请求数
# --------------------------------------------------------------------------
class _Stub(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            type(self).hits += 1
            body = json.dumps({
                "models": [
                    {"name": "qwen3:14b", "size": 9_000_000_000,
                     "details": {"parameter_size": "14B", "quantization_level": "Q4_K_M",
                                 "capabilities": ["completion", "tools"]}},
                    {"name": "bge-m3:latest", "size": 1_200_000_000,
                     "details": {"parameter_size": "567M", "quantization_level": "F16",
                                 "capabilities": ["embedding"]}},
                    {"name": "qwen2.5-coder:7b", "size": 4_700_000_000,
                     "details": {"parameter_size": "7B", "quantization_level": "Q4_K_M",
                                 "capabilities": ["completion"]}},
                ]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def seed_model_dirs(n_files: int = 400) -> None:
    """造一批嵌套的模型/数据集文件，让 rglob 有真实的遍历+stat 工作量。"""
    for base, sub in ((config.MODELS, ("hf", "ollama", "gguf")),
                      (config.DATASETS, ("corpus", "sft"))):
        for i in range(n_files):
            d = base / sub[i % len(sub)] / f"org{i % 7}" / f"repo{i % 23}"
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"shard-{i:04d}.bin"
            if not f.exists():
                f.write_bytes(b"\0" * 4096)


# --------------------------------------------------------------------------
# 两种组成
# --------------------------------------------------------------------------
def status_old() -> dict:
    """改动前的 api_status 组成（逐字复刻）。

    旧代码里 memory.stats() 与 downloader.disk_report() 各自调用一次
    config.disk_usage_gb()（实时 rglob），healthy() 与 list_models() 各打一次
    /api/tags，check_env() 每次探测 CUDA。这里显式补上两次实时 rglob，
    保证旧路径的成本与改动前完全一致（新代码里这两个函数已走 TTL 缓存，
    不补就等于故意让旧路径占便宜）。
    """
    env = ft._check_env_uncached()
    st = memory.stats()
    st["disk_gb"] = config._disk_usage_gb_uncached()      # 旧 stats 内部：实时 rglob
    dr = downloader.disk_report()
    dr["used_gb"] = config._disk_usage_gb_uncached()      # 旧 disk_report 内部：又一次
    return {
        "engine": evolve.engine.snapshot(),
        "stats": st,
        "llm": {"healthy": llm.healthy(),                 # HTTP #1
                "models": llm.list_models()},             # HTTP #2（同一端点，纯重复）
        "config": config.load(),
        "disk": dr,
        "finetune": {"ready": env["ready"], "gpu": env.get("gpu"), "cuda": env.get("cuda")},
        "finetune_state": ft.state(),
    }


def status_new() -> dict:
    """改动后的 api_status（直接调生产代码）。"""
    return main.api_status()


def _canon(d: dict) -> str:
    """规范化后比较：engine/stats 里的计时类字段（elapsed）可能因调用时刻不同而不同。"""
    d = json.loads(json.dumps(d, ensure_ascii=False, default=str))
    d.get("engine", {}).pop("elapsed", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)


def main_bench() -> bool:
    seed_model_dirs()
    srv, url = start_stub()
    config.update({"ollama_url": url})

    # 预热：建连接、 import torch、建向量缓存，别把一次性开销算进轮次
    status_old()
    status_new()

    ts_old: list[float] = []
    ts_new: list[float] = []
    mism = 0
    http_old = http_new = 0
    for i in range(ROUNDS):
        # 交替顺序；GAP>0 时按真实轮询节奏等一个周期，让 TTL 是否命中反映生产行为
        if GAP:
            time.sleep(GAP)
        _Stub.hits = 0
        t0 = time.perf_counter()
        d_old = status_old()
        t1 = time.perf_counter()
        hits_old = _Stub.hits

        if GAP:
            time.sleep(GAP)
        _Stub.hits = 0
        t2 = time.perf_counter()
        d_new = status_new()
        t3 = time.perf_counter()
        hits_new = _Stub.hits

        ts_old.append((t1 - t0) * 1000)
        ts_new.append((t3 - t2) * 1000)
        http_old += hits_old
        http_new += hits_new
        if _canon(d_old) != _canon(d_new):
            mism += 1
            if mism == 1:
                print("  [差异样例] old:", _canon(d_old)[:200])
                print("             new:", _canon(d_new)[:200])
        if i == 0:
            print(f"  第 1 轮 HTTP 请求数：旧 {hits_old} 次 vs 新 {hits_new} 次")

    srv.shutdown()

    pairs = list(zip(ts_old, ts_new))
    wins = sum(1 for o, n in pairs if n < o)
    med_o = sorted(ts_old)[len(ts_old) // 2]
    med_n = sorted(ts_new)[len(ts_new) // 2]
    print(f"轮询间隔 {GAP}s | 旧组成中位 {med_o:.1f}ms | 新组成中位 {med_n:.1f}ms "
          f"| 中位加速 {med_o / med_n:.2f}x")
    print(f"新组成更快轮次 {wins}/{len(pairs)} | payload 不一致轮次 {mism} "
          f"| Ollama 请求总数 旧 {http_old} vs 新 {http_new}")
    for i, (o, n) in enumerate(pairs):
        if n >= o:
            print(f"  [轮 {i + 1}] 旧 {o:.1f}ms ≤ 新 {n:.1f}ms")
    ok = wins == len(pairs) and mism == 0
    print("结果：", "通过" if ok else "未过")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main_bench() else 1)
