"""stats() 版本键缓存 交替 A/B 基准（无 Ollama；一次性临时 SQLite）。

测量 /api/status 最密调用的 memory.stats()：
  - 旧路径（_DISABLE_STATS_CACHE=True）：每次重跑 6 条 SQL + 磁盘调用
  - 新路径（版本键缓存 SQL + 8s 磁盘 TTL）：库没变过直接命中缓存
24 轮交替跑，避免本机噪声单边偏置；并对 DB 字段做逐字段等价校验。

用法：
  .venv\\Scripts\\python.exe tests\\benchmark_stats.py
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from backend import config, memory

N_POINTS = 2000
N_PER_ROUND = 12
ROUNDS = 24
TOPICS = [f"方向{i}" for i in range(8)]

# 磁盘部分：把 models/datasets/data 指到空临时目录，避免真实模型目录的
# 大批量 rglob 干扰对「SQL 缓存」这一项的测量（磁盘调用本身也被 TTL 收住）。
with tempfile.TemporaryDirectory(prefix="evomind_stats_bench_") as tmp:
    root = Path(tmp)
    (root / "models").mkdir()
    (root / "datasets").mkdir()
    config.DB_FILE = root / "bench.db"
    config.CONFIG_FILE = root / "config.json"
    config.MODELS = root / "models"
    config.DATASETS = root / "datasets"
    config.DATA = root
    config._cache = None

    rng = np.random.default_rng(7)
    vecs = rng.normal(size=(N_POINTS, 1024)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    c = memory.conn()
    c.executemany(
        "INSERT INTO knowledge(id,topic,claim,embedding,created_at) VALUES (?,?,?,?,?)",
        (
            (str(i), TOPICS[i % len(TOPICS)], f"测试知识点陈述内容 {i}", v.tobytes(), float(i))
            for i, v in enumerate(vecs)
        ),
    )
    c.commit()

    def db_fields(d: dict) -> dict:
        return {k: d[k] for k in d if k not in ("disk_gb", "budget_gb", "free_disk_gb", "ver")}

    # 等价性：旧路径一次重算 vs 新路径一次命中，DB 字段必须逐字段一致
    memory._DISABLE_STATS_CACHE = True
    a_ref = memory.stats()
    memory._DISABLE_STATS_CACHE = False
    b_ref = memory.stats()  # 顺带预热缓存
    equivalent = db_fields(a_ref) == db_fields(b_ref)

    a_rounds, b_rounds = [], []
    for r in range(ROUNDS):
        # A：旧路径，每次重算
        memory._DISABLE_STATS_CACHE = True
        t0 = time.perf_counter()
        for _ in range(N_PER_ROUND):
            memory.stats()
        a_rounds.append((time.perf_counter() - t0) * 1000 / N_PER_ROUND)
        # B：新路径，缓存命中
        memory._DISABLE_STATS_CACHE = False
        t0 = time.perf_counter()
        for _ in range(N_PER_ROUND):
            memory.stats()
        b_rounds.append((time.perf_counter() - t0) * 1000 / N_PER_ROUND)

    a_med = statistics.median(a_rounds)
    b_med = statistics.median(b_rounds)
    ratio = a_med / b_med if b_med > 0 else float("inf")
    print(json.dumps({
        "points": N_POINTS,
        "rounds": ROUNDS,
        "per_round_calls": N_PER_ROUND,
        "old_median_ms": round(a_med, 4),
        "new_median_ms": round(b_med, 4),
        "speedup_x": round(ratio, 2),
        "a_median_of_rounds_ms": round(statistics.median(a_rounds), 4),
        "b_median_of_rounds_ms": round(statistics.median(b_rounds), 4),
        "db_fields_equivalent": equivalent,
        "note": "命中缓存后 stats() 跳过 6 条 SQL，只做版本号比对 + 磁盘 TTL 读取",
    }, ensure_ascii=False))
    c.close()
    memory._local.c = None
    memory._invalidate()
    gc.collect()
