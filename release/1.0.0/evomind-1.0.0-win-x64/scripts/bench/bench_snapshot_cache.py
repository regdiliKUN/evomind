"""A/B：分析视图轮询时的聚合重算 —— 版本键缓存 vs 3s TTL 重算。

背景：/api/analytics 由仪表盘每 6 秒轮询一次（分析视图打开时）。旧实现用 3s TTL
缓存 snapshot，6s > 3s 永远等不到命中——每次轮询都把整份聚合重算一遍：
growth / topics / confidence / sources / topic_graph / sessions。
在 2000 条、1024 维的库上约 12ms，且随库内条数线性增长；每个打开分析页的
标签页每小时白烧 6 秒以上，纯粹因为「库根本没变」。

本基准在真实规模库上 24 轮交替对比：
  - 旧组成：逐字复刻改动前的 snapshot（各聚合函数直接调用 + 旧版 topic_graph，
            含每次全表建 rowid→topic map、Python 双重循环提边）
  - 新组成：生产代码 analytics.snapshot()（版本键缓存 + 安全 TTL）

核验：
  1. 两种组成返回的 payload 数据字段完全一致（等价性，ver 为新增字段单列）
  2. 写入后 snapshot()（缓存路径）与 snapshot(fresh=True)（强制重算路径）
     完全一致——证明写入版本号接线正确，缓存不会读过期数据
  3. 24/24 轮新组成更快（空闲轮询场景），并给出轮询风暴场景的总账
"""
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
ROUNDS = 24


# --------------------------------------------------------------------------
# 旧组成：改动前的 analytics.snapshot 逐字复刻（含旧版 topic_graph）
# --------------------------------------------------------------------------
def old_topic_graph(edge_min: float = 0.32) -> dict:
    stats = analytics.topic_stats()
    if len(stats) < 2:
        return {"nodes": stats, "edges": []}
    dim_row = memory.conn().execute(
        "SELECT length(embedding)/4 dim FROM knowledge WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    if not dim_row or not dim_row["dim"]:
        return {"nodes": stats, "edges": []}
    rowids, mat = memory._all_vectors(int(dim_row["dim"]))
    if not rowids or mat.shape[0] == 0:
        return {"nodes": stats, "edges": []}
    tmap = {
        r["rowid"]: r["topic"]
        for r in memory.conn().execute("SELECT rowid, topic FROM knowledge")
    }
    acc: dict[str, np.ndarray] = {}
    cnt: dict[str, int] = {}
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
    centroids = centroids / np.linalg.norm(centroids, axis=1, keepdims=True)
    sim = centroids @ centroids.T
    edges = []
    for i in range(len(topics)):
        for j in range(i + 1, len(topics)):
            w = float(sim[i, j])
            if w >= edge_min:
                edges.append({"a": topics[i], "b": topics[j], "w": round(w, 3)})
    return {"nodes": stats, "edges": edges}


def old_snapshot() -> dict:
    return {
        "total": memory.count_points(),
        "growth": analytics.growth_by_day(),
        "topics": analytics.topic_stats(),
        "confidence": analytics.confidence_hist(),
        "sources": analytics.source_breakdown(),
        "graph": old_topic_graph(),
        "sessions": analytics.session_timeline(),
    }


def strip_ver(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "ver"}


# --------------------------------------------------------------------------
# 造库：2000 条、40 个方向、1024 维，文本长度贴近真实语料
# --------------------------------------------------------------------------
def seed() -> None:
    rng = np.random.default_rng(7)
    for i in range(N):
        v = rng.standard_normal(DIM).astype(np.float32)
        v /= np.linalg.norm(v)
        memory.add_point(
            topic=f"方向_{(i % 40):02d} · 某个具体的学习方向名称",
            claim=f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。",
            detail="细节正文。" * 60,
            source_url=f"https://example.com/{i}", source_title=f"资料 {i}",
            source_kind="web", tags=["a", "b", "c"], confidence=0.6,
            vec=v.tolist(), dedup=False,
        )


t0 = time.perf_counter()
seed()
print(f"seed {N} 条耗时 {time.perf_counter() - t0:.1f}s")

# 预热：让向量缓存建好（两种组成共用，且都不是本次改动的对象）
memory._all_vectors(DIM)

# --------------------------------------------------------------------------
# 等价性核对（先于计时，避免「快但错了」）
# --------------------------------------------------------------------------
old = old_snapshot()
new = analytics.snapshot(fresh=True)
assert strip_ver(new) == old, "空闲状态下 旧组成 != 新组成（数据字段）"
assert "ver" in new and isinstance(new["ver"], int), "新组成应携带写入版本号"

# 写入后：缓存路径必须与强制重算路径一致（版本号接线验证）
rng = np.random.default_rng(11)
v = rng.standard_normal(DIM).astype(np.float32)
v /= np.linalg.norm(v)
pid = memory.add_point(
    topic="方向_99 · 写入后新增的方向", claim="这是写入后新增的一条知识点，用于验证缓存失效。",
    vec=v.tolist(), dedup=False,
)
cached_after_write = analytics.snapshot()
fresh_after_write = analytics.snapshot(fresh=True)
assert cached_after_write == fresh_after_write, "写入后缓存路径 != 强制重算路径"
assert fresh_after_write["total"] == N + 1, "写入后总数不对"
assert fresh_after_write["ver"] > new["ver"], "写入后版本号应递增"
memory.delete_point(pid)
assert analytics.snapshot() == analytics.snapshot(fresh=True), "删除后缓存路径 != 强制重算路径"
print("等价性核对通过：空闲一致 / 写入后缓存与重算一致 / 删除后一致 / 版本号递增")

# 删掉的这条会把版本号再 +1，回到干净状态
assert strip_ver(analytics.snapshot(fresh=True)) == old, "删除后数据字段应还原"
print(f"删除后还原一致（版本号现为 {memory.version()}）")

# --------------------------------------------------------------------------
# 24 轮交替 A/B
# --------------------------------------------------------------------------
old_ms: list[float] = []
new_ms: list[float] = []
wins = 0
for i in range(ROUNDS):
    t = time.perf_counter()
    old_snapshot()
    a = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    analytics.snapshot()
    b = (time.perf_counter() - t) * 1000
    old_ms.append(a)
    new_ms.append(b)
    if b < a:
        wins += 1

old_ms.sort()
new_ms.sort()
mo, mn = old_ms[ROUNDS // 2], new_ms[ROUNDS // 2]
print(f"\n24 轮交替 A/B（每轮 = 一次分析视图轮询的聚合）：")
print(f"  旧组成（3s TTL，每轮全量重算）  中位 {mo:7.2f} ms   最小 {old_ms[0]:7.2f} ms")
print(f"  新组成（版本键缓存，空闲命中）  中位 {mn:7.2f} ms   最小 {new_ms[0]:7.2f} ms")
print(f"  中位加速 {mo / mn:6.1f}x   新组成更快轮次 {wins}/{ROUNDS}")

# 轮询风暴：分析页开着不动，10 次轮询（60 秒）的总账
t = time.perf_counter()
for _ in range(10):
    old_snapshot()
storm_old = (time.perf_counter() - t) * 1000
t = time.perf_counter()
for _ in range(10):
    analytics.snapshot()
storm_new = (time.perf_counter() - t) * 1000
print(f"\n轮询风暴（10 次轮询 / 60 秒，库无变化）：")
print(f"  旧组成总耗时 {storm_old:8.1f} ms   新组成总耗时 {storm_new:6.2f} ms"
      f"   总账加速 {storm_old / storm_new:6.0f}x")
print(f"  折算每个打开分析页的标签页每小时（600 次轮询）："
      f"旧 {storm_old * 60 / 1000:.1f} s → 新 {storm_new * 60 / 1000:.2f} s")
