"""学习分析：把知识库喂给「分析」视图的各种聚合。

全部是只读查询，按需计算（打开分析页才跑），不常驻、不影响导入/检索性能。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import memory


def growth_by_day(days: int = 60) -> list[dict]:
    """每天新增的知识点数 + 当日累计，供折线图用。"""
    c = memory.conn()
    rows = c.execute(
        "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') d, COUNT(*) n "
        "FROM knowledge WHERE created_at > 0 "
        f"GROUP BY d ORDER BY d DESC LIMIT {int(days)}"
    ).fetchall()
    series = [(r["d"], r["n"]) for r in reversed(rows)]
    # 反推累计：从当前总数往前减
    total = c.execute("SELECT COUNT(*) n FROM knowledge").fetchone()["n"]
    cum = total
    out = []
    for d, n in reversed(series):
        cum -= n
        out.append({"day": d, "added": n, "cumulative": cum + n})
    out.reverse()
    return out


def topic_stats() -> list[dict]:
    """每个学习方向：知识点数 + 平均可信度，供分布条与网络节点用。"""
    rows = memory.conn().execute(
        "SELECT topic, COUNT(*) n, AVG(confidence) ac FROM knowledge "
        "GROUP BY topic ORDER BY n DESC LIMIT 40"
    ).fetchall()
    return [
        {"topic": r["topic"], "count": r["n"], "avg_conf": round(float(r["ac"] or 0), 3)}
        for r in rows
    ]


def confidence_hist(bins: int = 10) -> list[dict]:
    """可信度直方图（0~1 均分 bins 个桶）。"""
    rows = memory.conn().execute(
        "SELECT CAST(MIN(0.9999, confidence) * ? AS INT) b, COUNT(*) n "
        "FROM knowledge GROUP BY b",
        (bins,),
    ).fetchall()
    counts = {r["b"]: r["n"] for r in rows}
    return [
        {"lo": round(i / bins, 2), "hi": round((i + 1) / bins, 2), "n": counts.get(i, 0)}
        for i in range(bins)
    ]


def source_breakdown() -> list[dict]:
    rows = memory.conn().execute(
        "SELECT COALESCE(NULLIF(source_kind,''),'未知') k, COUNT(*) n "
        "FROM knowledge GROUP BY k ORDER BY n DESC"
    ).fetchall()
    return [{"kind": r["k"], "count": r["n"]} for r in rows]


def topic_graph(edge_min: float = 0.32) -> dict:
    """主题语义网络：每个主题一个节点（用其知识点向量的质心表示），

    两主题的质心余弦相似度 >= edge_min 才连边。前端据此做力导向布局。
    质心来自已归一化的向量矩阵（_all_vectors 缓存的即是单位向量），
    所以质心点积就是余弦相似度，O(T²) 且 T 通常 < 40，足够快。
    """
    stats = topic_stats()
    if len(stats) < 2:
        return {"nodes": stats, "edges": []}
    # 向量维度取决于 embedding 模型（bge-m3=1024，MiniLM=384），从库里读真实值
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
    # 按主题累加质心
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


def search_explain(query: str, k: int = 8) -> dict:
    """检索命中可视化：返回 top-k 与每条的匹配度，以及查询本身的向量。

    让「为什么是这些结果」看得见——前端用匹配度画条形，用节点图展示
    查询与各命中点的相似关系。
    """
    hits = memory.search(query, k=k)
    return {
        "query": query,
        "hits": [
            {
                "claim": h["claim"],
                "topic": h.get("topic", ""),
                "score": h.get("score", 0),
                "source_title": h.get("source_title", ""),
            }
            for h in hits
        ],
    }


def snapshot() -> dict:
    """一次性返回分析视图所需的全部数据。"""
    return {
        "total": memory.count_points(),
        "growth": growth_by_day(),
        "topics": topic_stats(),
        "confidence": confidence_hist(),
        "sources": source_breakdown(),
        "graph": topic_graph(),
    }
