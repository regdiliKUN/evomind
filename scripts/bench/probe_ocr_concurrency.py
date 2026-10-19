"""探查 OCR 并发到底能不能并行：同一引擎 vs 每线程一个引擎，测实际加速比。

    .venv/Scripts/python.exe scripts/bench/probe_ocr_concurrency.py
"""
from __future__ import annotations

import sys
import threading
import time

import _env  # noqa: F401

_env.isolate()


def load_pages(n: int, dpi: int = 200):
    import pymupdf

    from backend import ocr as ocrmod

    doc = pymupdf.open(str(_env.FIXTURES / "scanned_book.pdf"))
    out = []
    for i in range(n):
        _, arr = ocrmod.render_page(doc, i, dpi)
        out.append(arr)
    doc.close()
    return out


def run(mode: str, workers: int, pages: int):
    from backend import ocr as ocrmod

    imgs = load_pages(pages)

    if mode == "shared":
        eng = ocrmod._rapid()
        engines = [eng] * workers
    else:  # per-thread
        ocrmod._ocr_engine = None
        engines = []
        for _ in range(workers):
            ocrmod._ocr_engine = None          # 强制 _rapid() 新建
            engines.append(ocrmod._rapid())
        ocrmod._ocr_engine = engines[0]

    idx = [0]
    lock = threading.Lock()
    chars = [0]

    def worker(e):
        while True:
            with lock:
                i = idx[0]
                if i >= len(imgs):
                    return
                idx[0] += 1
            t = ocrmod.ocr_rapid(imgs[i]) if e is None else _recognize(e, imgs[i])
            with lock:
                chars[0] += len(t)

    def _recognize(e, arr):
        res, _ = e(arr)
        if not res:
            return ""
        return "\n".join(str(r[1]) for r in res)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(engines[i],)) for i in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    el = time.perf_counter() - t0
    ppm = pages / el * 60
    extra = f" · 加速比 {ppm / BASE:5.2f}x" if BASE else ""
    print(f"  {mode:<10} workers={workers}  {pages:>3} 页 · {el:6.2f}s · "
          f"{ppm:6.1f} 页/分钟{extra}")
    return el, ppm


BASE = 0.0

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--workers", default="1,2,3,4")
    args = ap.parse_args()

    print("=== 并发探查（页数固定，看线程数能否换来吞吐）===")
    # 先测 1 线程做基准
    _, base_ppm = run("shared", 1, args.pages)
    BASE = args.pages / base_ppm * 60
    print(f"  （基准：1 线程 {base_ppm:.1f} 页/分钟）\n")
    for w in [int(x) for x in args.workers.split(",")]:
        if w == 1:
            continue
        run("shared", w, args.pages)
    print()
    for w in [int(x) for x in args.workers.split(",")]:
        run("per-thread", w, args.pages)
    sys.exit(0)
