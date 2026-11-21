"""Seeded 10k x 1024 retrieval benchmark (no Ollama; disposable SQLite).

Run from the repository: python tests/benchmark_retrieval.py
Optionally pass another checkout path to compare its implementation.
"""
import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
import gc
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
import numpy as np
from backend import config, memory

with tempfile.TemporaryDirectory(prefix="evomind_bench_") as tmp:
    config.DB_FILE = Path(tmp) / "bench.db"
    config.CONFIG_FILE = Path(tmp) / "config.json"
    rng = np.random.default_rng(42)
    vectors = rng.normal(size=(10000, 1024)).astype(np.float32)
    query = rng.normal(size=1024).astype(np.float32).tolist()
    c = memory.conn()
    c.executemany(
        "INSERT INTO knowledge(id,topic,claim,embedding,created_at) VALUES (?,?,?,?,?)",
        ((str(i), "test", f"Benchmark claim {i}", v.tobytes(), i)
         for i, v in enumerate(vectors)),
    )
    c.commit()
    memory.search("benchmark", k=8, vec=query)
    timings = []
    for _ in range(30):
        start = time.perf_counter()
        result = memory.search("benchmark", k=8, vec=query)
        timings.append((time.perf_counter() - start) * 1000)
    print(json.dumps({"vectors": 10000, "dimensions": 1024, "runs": 30,
                      "median_ms": round(statistics.median(timings), 3),
                      "best_ms": round(min(timings), 3),
                      "top_ids": [p["id"] for p in result]}))
    c.close()
    memory._local.c = None
    memory._invalidate()
    gc.collect()
