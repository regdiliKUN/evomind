"""第九轮 A/B：HTTP 客户端「每次新建」 vs 「共享连接池」。

本地起三个 stub：
  1. Ollama 桩（/api/chat、/api/embed）——覆盖 llm.chat / llm.embed
  2. 站点桩（GET / 返回 HTML）——覆盖 fetcher.fetch 通用抓取路径
  3. ZBrushCentral 桩（monkeypatch search.ZBC_BASE）——覆盖 search 的列表拉取

每轮负载模仿自主进化一轮的 HTTP 形态：4 次对话 + 2 次嵌入 + 6 次抓取 + 1 次论坛索引。
通过 httpc.FORCE_NEW_CLIENT 开关在同一份生产代码上切换两种行为，
交替顺序执行，配对比较；并核对两种模式返回的载荷一致。

用法：.venv/Scripts/python.exe scripts/bench/bench_conn_pool.py [rounds]
"""
from __future__ import annotations

import gc
import json
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env

_env.isolate()
_env.fake_llm()

from backend import config  # noqa: E402

# ---------------------------------------------------------------- Ollama 桩
class _OllamaStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b"{}"
        try:
            req = json.loads(body)
        except Exception:
            req = {}
        if self.path == "/api/embed":
            n = len(req.get("input") or [])
            payload = {"embeddings": [[0.25] * 8 for _ in range(n)]}
        else:
            payload = {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"points": [{"claim": "桩返回的知识点内容足够长用于连接池基准测试" * 3}]},
                        ensure_ascii=False,
                    ),
                }
            }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# ---------------------------------------------------------------- 站点桩
_PAGE = (
    "<html><head><title>桩页面</title></head><body><article>"
    + "连接池基准用的正文内容，需要足够长才能通过抓取器的最短长度校验。" * 8
    + "</article></body></html>"
).encode()


class _SiteStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)


# ---------------------------------------------------------------- ZBC 桩
class _ZbcStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        payload = {
            "topic_list": {
                "topics": [
                    {"id": 100 + i, "title": f"DynaMesh workflow issue {i}", "posts_count": 5 + i}
                    for i in range(5)
                ]
            }
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def workload() -> dict:
    """一轮负载：返回可比较的载荷摘要。"""
    from backend import fetcher, llm, search

    out = {}
    # 4 次对话（规划 1 + 提炼 2 + 收尾 1）
    chats = []
    for i in range(4):
        chats.append(
            llm.chat(
                [{"role": "user", "content": f"连接池基准对话 {i} " + "内容" * 30}],
                timeout=30,
            )[:40]
        )
    out["chat"] = chats
    # 2 次嵌入
    out["embed"] = [len(v) for v in llm.embed([f"嵌入文本 {i} " + "字" * 20 for i in range(8)])]
    # 6 次抓取
    docs = [fetcher.fetch(f"http://127.0.0.1:{_SITE_PORT}/page/{i}") for i in range(6)]
    out["fetch"] = [(d["ok"], len(d["text"])) for d in docs]
    # 1 次论坛索引拉取（ZBC 桩）
    hits = search.search_zbrushcentral("dynamesh workflow", limit=3)
    out["zbc"] = len(hits)
    return out


def main() -> int:
    global _SITE_PORT
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 24

    ollama = _serve(_OllamaStub)
    site = _serve(_SiteStub)
    zbc = _serve(_ZbcStub)
    _SITE_PORT = site.server_address[1]

    config.update({"ollama_url": f"http://127.0.0.1:{ollama.server_address[1]}"})

    from backend import httpc, search

    # ZBC 桩接入（仅基准）：把生产代码里的论坛基址指向本地桩
    search.ZBC_BASE = f"http://127.0.0.1:{zbc.server_address[1]}"

    def reset_zbc_cache():
        search._zbc_cache.update(ts=0.0, topics=[])

    def run_once() -> tuple[float, dict]:
        reset_zbc_cache()
        t0 = time.perf_counter()
        payload = workload()
        return (time.perf_counter() - t0) * 1000, payload

    # 预热：建好共享客户端、桩连接就绪
    httpc.FORCE_NEW_CLIENT = False
    warm, ref = run_once()
    print(f"预热轮（共享）: {warm:.1f}ms")

    ts_old: list[float] = []
    ts_new: list[float] = []
    mism = 0
    for i in range(rounds):
        # 交替先后，抵消漂移
        if i % 2 == 0:
            first, second = True, False
        else:
            first, second = False, True

        if first:
            httpc.FORCE_NEW_CLIENT = True
            t_old, p_old = run_once()
            gc.collect()
            httpc.FORCE_NEW_CLIENT = False
            t_new, p_new = run_once()
        else:
            httpc.FORCE_NEW_CLIENT = False
            t_new, p_new = run_once()
            httpc.FORCE_NEW_CLIENT = True
            t_old, p_old = run_once()
            gc.collect()

        ts_old.append(t_old)
        ts_new.append(t_new)
        if p_old != p_new:
            mism += 1
            if mism == 1:
                print("  [载荷差异样例] old:", str(p_old)[:200])
                print("                new:", str(p_new)[:200])

    httpc.FORCE_NEW_CLIENT = False
    ollama.shutdown()
    site.shutdown()
    zbc.shutdown()

    pairs = list(zip(ts_old, ts_new))
    wins = sum(1 for o, n in pairs if n < o)
    med_o = statistics.median(ts_old)
    med_n = statistics.median(ts_new)
    print(f"每次新建 Client 中位: {med_o:.1f}ms | 共享连接池中位: {med_n:.1f}ms | 中位加速 {med_o / med_n:.2f}x")
    print(f"共享更快轮次 {wins}/{len(pairs)} | 载荷不一致轮次 {mism}")
    for i, (o, n) in enumerate(pairs):
        if n >= o:
            print(f"  [轮 {i + 1}] 新建 {o:.1f}ms ≤ 共享 {n:.1f}ms")
    ok = wins == len(pairs) and mism == 0
    print("结果：", "通过" if ok else "未过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
