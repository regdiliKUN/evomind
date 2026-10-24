"""第十三轮补充测量：近重复扫描在 1 万条 / 60 方向规模下的耗时与封顶行为。"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()
_env.fake_llm()

import numpy as np  # noqa: E402

from backend import analytics, memory  # noqa: E402

N, DIM, TOPICS = 10000, 1024, 60
rng = np.random.default_rng(31)
vecs = rng.standard_normal((N, DIM)).astype(np.float32)
vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
t0 = time.perf_counter()
for i in range(N):
    memory.add_point(
        topic=f"方向_{(i % TOPICS):02d} · 某个具体的学习方向名称",
        claim=f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。",
        detail="细节正文。" * 60, source_url=f"https://example.com/{i}",
        source_title="资料", source_kind="web", tags=["a", "b"], confidence=0.6,
        vec=vecs[i].tolist(), dedup=False,
    )
print(f"seed {N} 条用时 {time.perf_counter() - t0:.1f}s")
analytics._reset_caches()

t0 = time.perf_counter()
r = memory.near_duplicates(threshold=0.9, limit=30)
cold = (time.perf_counter() - t0) * 1000
print(f"近重复冷扫（1 万条 / {TOPICS} 方向）：{cold:.1f} ms，对数 {len(r['pairs'])}，扫描 {r['scanned']} 条，封顶方向 {r['capped']} 个")
t0 = time.perf_counter()
memory.near_duplicates(threshold=0.9, limit=30)
print(f"缓存命中：{(time.perf_counter() - t0) * 1000:.3f} ms")
# 写入后应重算
memory.add_point(
    topic="方向_00 · 某个具体的学习方向名称", claim="这是第 9999 条知识点，claim 长度约 40 字，用于模拟真实语料。",
    detail="d", source_url="https://example.com/new", source_title="t", source_kind="web",
    tags=[], confidence=0.6, vec=vecs[0].tolist(), dedup=False,
)
t0 = time.perf_counter()
memory.near_duplicates(threshold=0.9, limit=30)
print(f"写入一次后重扫：{(time.perf_counter() - t0) * 1000:.1f} ms")
