"""Probe: 主题过滤检索 —— 朴素(DB查询+矩阵复制) vs 优化(内存索引+掩码) 的耗时信号。

只做探测，不污染 memory.py。两条路径都用 memory 的 _all_vectors 与真实 DB 状态。
"""
import sys, time, random
sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")
import _env; _env.isolate(); _env.fake_llm()
import numpy as np
from backend import memory, llm


def seed(n=4000, topics=8):
    for _ in range(n):
        t = f"方向{random.randint(0, topics-1)}"
        memory.add_point(topic=t, claim=f"知识点内容 {random.random()} 用于探测主题过滤检索的去 DB 化与掩码相似度性能差异是否足够显著。",
                         detail="细节 "*10, source_kind="web", confidence=0.6, dedup=False)


def build_topic_index():
    idx = {}
    for r in memory.conn().execute("SELECT rowid, topic FROM knowledge"):
        idx.setdefault(r["topic"], set()).add(r["rowid"])
    return idx


def naive(topic, vec):
    rowids, mat = memory._all_vectors(len(vec))
    allowed = {r[0] for r in memory.conn().execute("SELECT rowid FROM knowledge WHERE topic=?", (topic,))}
    indices = [i for i, rid in enumerate(rowids) if rid in allowed]
    sub = [rowids[i] for i in indices]
    m2 = mat[indices]
    hits = memory.similarity(vec, sub, m2, k=8, normalized=True)
    return [rid for rid, _ in hits]


def optimized(topic, vec, idx):
    rowids, mat = memory._all_vectors(len(vec))
    allowed = idx.get(topic, set())
    if not allowed:
        return []
    mask = np.array([rid in allowed for rid in rowids], dtype=bool)
    sims = (mat @ np.asarray([vec], dtype=np.float32).T).ravel()
    sims = sims.copy()
    sims[~mask] = -1e9
    cnt = min(8, len(rowids))
    cand = np.argpartition(-sims, cnt - 1)[:cnt]
    order = cand[np.argsort(-sims[cand], kind="stable")]
    return [rowids[i] for i in order]


def main():
    seed()
    topics = [t for t, in memory.conn().execute("SELECT DISTINCT topic FROM knowledge")]
    idx = build_topic_index()
    print("topics:", len(topics), "| total points:", memory.count_points())
    ROUNDS = 24
    K = 120  # 每轮主题过滤检索次数
    wins = 0
    ts_n, ts_o = [], []
    mism = 0
    for r in range(ROUNDS):
        qs = [f"查询 {random.random()} 主题过滤探测题面用于压测" for _ in range(K)]
        # 朴素
        t0 = time.perf_counter()
        for q in qs:
            v = llm.embed([q])[0]
            t = random.choice(topics)
            naive(t, v)
        tn = (time.perf_counter() - t0) * 1000
        # 优化
        t0 = time.perf_counter()
        for q in qs:
            v = llm.embed([q])[0]
            t = random.choice(topics)
            optimized(t, v, idx)
        to = (time.perf_counter() - t0) * 1000
        ts_n.append(tn); ts_o.append(to)
        if to < tn: wins += 1
    med_n = sorted(ts_n)[ROUNDS // 2]
    med_o = sorted(ts_o)[ROUNDS // 2]
    print(f"朴素中位 {med_n:.1f}ms | 优化中位 {med_o:.1f}ms | 加速 {med_n/med_o:.2f}x | 优化更快轮次 {wins}/{ROUNDS}")
    # 等价性：抽样 50 个查询比对命中集合
    for _ in range(50):
        t = random.choice(topics)
        q = f"等价性校验题面 {random.random()} 用于确认两条路径返回相同命中"
        v = llm.embed([q])[0]
        a = set(naive(t, v)); b = set(optimized(t, v, idx))
        if a != b:
            mism += 1
    print("等价性抽样不一致:", mism)


if __name__ == "__main__":
    main()
