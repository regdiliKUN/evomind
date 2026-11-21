"""端到端 OCR 吞吐测试：走真实 API，边跑边采样 GPU 利用率。

    .venv/Scripts/python.exe scripts/bench_pipeline.py --book-id <id> --pages 170-240
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8765"


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def gpu_util() -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        u, m = out.stdout.strip().splitlines()[0].split(",")
        return float(u), float(m)
    except Exception:
        return -1.0, -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-id", required=True)
    ap.add_argument("--pages", default="170-240")
    ap.add_argument("--engine", default="rapid")
    ap.add_argument("--no-skip", action="store_true", help="重跑已识别过的页，便于 A/B 对比")
    args = ap.parse_args()

    st = api("/api/ocr/device")
    print("后端状态:", json.dumps(st, ensure_ascii=False))

    start = api(f"/api/library/books/{args.book_id}/ocr", "POST",
                {"pages": args.pages, "engine": args.engine,
                 "skip_done": not args.no_skip})
    print("启动:", start)
    if not start.get("ok"):
        return 1

    utils: list[float] = []
    mems: list[float] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            u, m = gpu_util()
            if u >= 0:
                utils.append(u)
                mems.append(m)
            stop.wait(0.5)

    threading.Thread(target=sample, daemon=True).start()

    t0 = time.time()
    last = 0
    while True:
        s = api("/api/ocr/state")
        done = s.get("done", 0)
        if done != last:
            el = time.time() - t0
            print(f"  {done}/{s.get('total')} 页 · {el:5.1f}s · "
                  f"{done / el * 60:5.1f} 页/分钟 · 最近一页 {s.get('elapsed')}s")
            last = done
        if not s.get("running") and not s.get("batch"):
            break
        time.sleep(1.0)

    stop.set()
    total = time.time() - t0
    s = api("/api/ocr/state")
    done = s.get("done", 0)
    print("\n===== 结果 =====")
    print(f"完成 {done} 页 / 耗时 {total:.1f}s = {done / total * 60:.0f} 页/分钟")
    if utils:
        print(f"GPU 利用率 平均 {statistics.mean(utils):.0f}% / 峰值 {max(utils):.0f}% · "
              f"显存峰值 {max(mems):.0f}MB · 采样 {len(utils)} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
