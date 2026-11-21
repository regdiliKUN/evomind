"""负载基准 · 批量知识入库与检索并发（AUDIT_REVIEW §10 P3「无负载基准」）。

    .venv\\Scripts\\python.exe tests\\bench_load_ingest_search.py [--points 17426] [--readers 1,4,8]
        [--searches 200] [--batches 20] [--batch-size 30] [--json]

临时目录里建一份真实 schema 的大库（默认 1.7 万条、1024 维向量），不调用 Ollama：
入库点自带 vec、检索直接传 vec（绕过检索结果缓存，测的是真实计算路径）。

三个阶段，各自报告 p50 / p95 / 最大延迟与吞吐，另跑一遍 tracemalloc 量 Python 峰值：

  search-only  R 个读线程并发 memory.search（半数带 topic 过滤）
  ingest-only  1 个写线程连续 memory.add_points_batch（每批 batch-size 条，含查重）
  mixed        R 个读线程 + 1 个写线程同时跑：读延迟受写入影响多少、写入被读拖慢多少
"""
from __future__ import annotations

import argparse
import gc
import json
import shutil
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
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402

from backend import config, llm, memory  # noqa: E402
from bench_load_backup import _close_memory, build_db  # noqa: E402

DIM = 1024


def summarize(samples: list[float], wall: float | None = None, units: int | None = None) -> dict:
    if not samples:
        return {"n": 0}
    s = sorted(samples)
    p95 = s[min(len(s) - 1, max(0, int(round(.95 * (len(s) - 1)))))]
    out = {"n": len(s), "p50_ms": round(statistics.median(s) * 1000, 2),
           "p95_ms": round(p95 * 1000, 2), "max_ms": round(s[-1] * 1000, 2)}
    if wall:
        out["per_s"] = round((units if units is not None else len(s)) / wall, 1)
    return out


def unit(rng: np.random.Generator) -> list[float]:
    v = rng.normal(size=DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return v.tolist()


def make_batch(rng: np.random.Generator, tag: str, size: int) -> list[dict]:
    return [{
        "topic": f"基准主题 {i % 40}",
        "claim": f"并发入库基准 {tag}-{i}：参数取 {i * 7} 单位时吞吐提升 {i % 13}%，在固定负载下可复现。",
        "detail": "并发基准补充说明，模拟真实知识点的 detail 长度。" * 3,
        "source_url": f"https://example.com/load/{tag}/{i}", "source_title": f"load {tag}",
        "source_kind": "web", "tags": ["load"], "confidence": .7, "vec": unit(rng),
    } for i in range(size)]


def reader(n: int, seed: int, out: list, stop: threading.Event | None = None) -> None:
    rng = np.random.default_rng(seed)
    try:
        for i in range(n):
            if stop is not None and stop.is_set():
                break
            topic = f"基准主题 {i % 40}" if i % 2 else None
            t0 = time.perf_counter()
            memory.search("负载基准查询", k=8, topic=topic, vec=unit(rng))
            out.append(time.perf_counter() - t0)
    finally:
        _close_memory()


def writer(batches: int, size: int, seed: int, out: list, added: list) -> None:
    rng = np.random.default_rng(seed)
    try:
        for b in range(batches):
            pts = make_batch(rng, f"s{seed}b{b}", size)
            t0 = time.perf_counter()
            r = memory.add_points_batch(pts, session_id="load")
            out.append(time.perf_counter() - t0)
            added.append(int(r.get("added", 0)))
    finally:
        _close_memory()


def phase_search(readers: int, searches: int) -> dict:
    lat: list[float] = []
    threads = [threading.Thread(target=reader, args=(searches, 100 + i, lat)) for i in range(readers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return summarize(lat, time.perf_counter() - t0)


def phase_ingest(batches: int, size: int, seed: int) -> dict:
    lat: list[float] = []
    added: list[int] = []
    t0 = time.perf_counter()
    writer(batches, size, seed, lat, added)
    wall = time.perf_counter() - t0
    return summarize(lat, wall, sum(added)) | {"added": sum(added)}


def phase_mixed(readers: int, searches: int, batches: int, size: int, seed: int) -> dict:
    rlat: list[float] = []
    wlat: list[float] = []
    added: list[int] = []
    stop = threading.Event()
    rs = [threading.Thread(target=reader, args=(searches, 500 + i, rlat, stop)) for i in range(readers)]
    w = threading.Thread(target=writer, args=(batches, size, seed, wlat, added))
    t0 = time.perf_counter()
    for t in rs:
        t.start()
    w.start()
    w.join()
    stop.set()
    for t in rs:
        t.join()
    wall = time.perf_counter() - t0
    return {"search": summarize(rlat, wall), "ingest": summarize(wlat, wall, sum(added)) | {"added": sum(added)}}


def traced(fn, *a) -> float:
    gc.collect()
    tracemalloc.start()
    try:
        fn(*a)
        return round(tracemalloc.get_traced_memory()[1] / 1e6, 1)
    finally:
        tracemalloc.stop()


def run(work: Path, n_points: int, levels: list[int], searches: int, batches: int, size: int) -> dict:
    kdir = work / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    db = kdir / "knowledge.db"
    probe = np.random.default_rng(1).normal(size=DIM).astype(np.float32)
    probe /= np.linalg.norm(probe)
    with patch.multiple(config, DATA=work, LOGS=work / "logs", MODELS=work / "models",
                        DATASETS=work / "datasets", KNOWLEDGE=kdir,
                        CONFIG_FILE=work / "config.json", DB_FILE=db, _cache=None), \
            patch.object(llm, "embed", lambda texts, **kw: [probe.tolist() for _ in texts]):
        memory._invalidate()
        memory._bump_search_cache()
        scale = build_db(db, n_points, 0, 0)
        out: dict = {"scale": scale}
        t0 = time.perf_counter()
        memory.search("预热", k=8, vec=unit(np.random.default_rng(0)))  # 冷启动：整表向量载入
        out["cold_first_search_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        _close_memory()

        out["search_only"] = {str(r): phase_search(r, searches) for r in levels}
        out["ingest_only"] = phase_ingest(batches, size, seed=11)
        out["mixed"] = {str(r): phase_mixed(r, searches, batches, size, seed=20 + r) for r in levels}
        out["peak_py_mb"] = {
            "search_x4": traced(phase_search, 4, max(20, searches // 4)),
            "ingest": traced(phase_ingest, max(2, batches // 4), size, 99),
        }
        _close_memory()
        memory._invalidate()
        memory._bump_search_cache()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=17426)
    ap.add_argument("--readers", default="1,4,8")
    ap.add_argument("--searches", type=int, default=200, help="每个读线程的检索次数")
    ap.add_argument("--batches", type=int, default=20)
    ap.add_argument("--batch-size", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    levels = [int(x) for x in args.readers.split(",") if x.strip()]

    work = Path(tempfile.mkdtemp(prefix="evomind-bench-load-"))
    try:
        out = run(work, args.points, levels, args.searches, args.batches, args.batch_size)
    finally:
        _close_memory()
        gc.collect()
        shutil.rmtree(work, ignore_errors=True)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    s = out["scale"]
    print(f"规模：{s['points']} 条 · 库 {s['db_mb']} MB · 冷启动首检 {out['cold_first_search_ms']} ms")
    for r, v in out["search_only"].items():
        print(f"仅检索  R={r:<2} p50 {v['p50_ms']} ms · p95 {v['p95_ms']} · max {v['max_ms']} · {v['per_s']} 次/s")
    v = out["ingest_only"]
    print(f"仅入库  每批 {args.batch_size} 条 p50 {v['p50_ms']} ms · p95 {v['p95_ms']} · max {v['max_ms']}"
          f" · {v['per_s']} 条/s（新增 {v['added']}）")
    for r, m in out["mixed"].items():
        a, b = m["search"], m["ingest"]
        print(f"混合    R={r:<2} 检索 p50 {a['p50_ms']} · p95 {a['p95_ms']} · max {a['max_ms']} ms · {a['per_s']} 次/s"
              f" ｜ 入库 p50 {b['p50_ms']} · p95 {b['p95_ms']} · max {b['max_ms']} ms · {b['per_s']} 条/s")
    print(f"Python 峰值：检索×4 {out['peak_py_mb']['search_x4']} MB · 入库 {out['peak_py_mb']['ingest']} MB")


if __name__ == "__main__":
    main()
