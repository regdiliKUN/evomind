"""拆解 analytics.snapshot 的 11ms 花在哪。"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()

import numpy as np  # noqa: E402

from backend import analytics, memory  # noqa: E402

N, DIM = 2000, 1024
rng = np.random.default_rng(7)
for i in range(N):
    v = rng.standard_normal(DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    memory.add_point(
        topic=f"方向_{(i % 40):02d} · 某个具体的学习方向名称",
        claim=f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。",
        detail="细节正文。" * 60,
        vec=v.tolist(), dedup=False,
    )
memory._bump_search_cache()


def bench(name, fn, rounds=12):
    fn()
    ts = []
    for _ in range(rounds):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    print(f"{name:44s} 中位 {ts[len(ts)//2]:7.2f} ms")


bench("growth_by_day()", analytics.growth_by_day)
bench("topic_stats()", analytics.topic_stats)
bench("confidence_hist()", analytics.confidence_hist)
bench("source_breakdown()", analytics.source_breakdown)
bench("session_timeline()", analytics.session_timeline)
bench("topic_graph()", analytics.topic_graph)
bench("memory.count_points()", memory.count_points)
bench("snapshot(fresh=True) 整体", lambda: analytics.snapshot(fresh=True))

# topic_graph 内部拆解
tmap_sql = "SELECT rowid, topic FROM knowledge"
bench("  └ SELECT rowid,topic 全表 + 建 dict", lambda: {r["rowid"]: r["topic"] for r in memory.conn().execute(tmap_sql)})
rowids, mat = memory._all_vectors(DIM)
bench("  └ _all_vectors(1024)（已缓存）", lambda: memory._all_vectors(DIM))
tmap = {r["rowid"]: r["topic"] for r in memory.conn().execute(tmap_sql)}


def accum_python():
    acc, cnt = {}, {}
    for rid, vec in zip(rowids, mat):
        t = tmap.get(rid)
        if not t:
            continue
        if t not in acc:
            acc[t] = np.zeros(mat.shape[1], dtype=np.float32)
            cnt[t] = 0
        acc[t] += vec
        cnt[t] += 1
    topics = list(acc.keys())
    centroids = np.stack([acc[t] / cnt[t] for t in topics], axis=0)
    return centroids / np.linalg.norm(centroids, axis=1, keepdims=True)


bench("  └ 质心累加（Python 循环）", accum_python)
