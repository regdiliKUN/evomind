"""探测：httpx.Client 每次新建 vs 共享连接池，在本地 stub 服务上的差异。

用法：.venv/Scripts/python.exe scripts/bench/probe_conn.py [rounds]
"""
from __future__ import annotations

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


class _Stub(BaseHTTPRequestHandler):
    """最小 Ollama 桩：/api/chat 与 /api/embed，带少量处理耗时。"""

    hits = 0
    lock = threading.Lock()

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b"{}"
        with self.__class__.lock:
            self.__class__.hits += 1
        try:
            req = json.loads(body)
        except Exception:
            req = {}
        if self.path == "/api/embed":
            n = len(req.get("input") or [])
            payload = {"embeddings": [[0.1] * 8 for _ in range(n)]}
        else:
            payload = {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"points": []}, ensure_ascii=False),
                }
            }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    config.update({"ollama_url": f"http://127.0.0.1:{srv.server_address[1]}"})

    import httpx

    from backend import llm

    msgs = [{"role": "user", "content": "探测连接池开销 " * 20}]

    def new_client_call():
        with httpx.Client(base_url=config.get("ollama_url"), trust_env=False) as c:
            c.post("/api/chat", json={"model": "m", "messages": msgs, "stream": False})

    def shared_call(client):
        client.post("/api/chat", json={"model": "m", "messages": msgs, "stream": False})

    shared = httpx.Client(base_url=config.get("ollama_url"), trust_env=False)

    t_new, t_shared = [], []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(10):
            new_client_call()
        t_new.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        for _ in range(10):
            shared_call(shared)
        t_shared.append((time.perf_counter() - t0) * 1000)

    shared.close()
    srv.shutdown()

    med_new = statistics.median(t_new)
    med_sh = statistics.median(t_shared)
    wins = sum(1 for a, b in zip(t_new, t_shared) if b < a)
    print(f"每次新建 Client（10 次调用）中位: {med_new:.1f}ms")
    print(f"共享连接池 Client（10 次调用）中位: {med_sh:.1f}ms")
    print(f"中位加速 {med_new / med_sh:.2f}x | 共享更快轮次 {wins}/{rounds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
