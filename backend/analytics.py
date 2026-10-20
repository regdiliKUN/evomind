"""学习分析：把知识库喂给「分析」视图的各种聚合。

全部是只读查询，按需计算（打开分析页才跑），不常驻、不影响导入/检索性能。
"""
from __future__ import annotations

import math
import time
from datetime import date, timedelta
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


# 主题语义网络的版本键缓存：图是库内容的确定函数，库没变过结果就不会变。
# 分析视图 6 秒轮询一次，旧实现每轮都全表建 map + 质心累加 + T² 遍历（2000 条约
# 6.5ms），而其中绝大部分轮次库根本没有变化。按 memory.version() 缓存后，
# 没写过的轮次直接复用，写过的轮次才重算——输出与旧实现逐字段一致。
_GRAPH_CACHE: dict = {"ver": -1, "edge_min": None, "val": None}
# rowid → topic 的全表映射同样按版本缓存（旧实现每次调用都重扫全表建 dict）
_TMAP_CACHE: dict = {"ver": -1, "val": None}


def _rowid_topic_map() -> dict:
    ver = memory.version()
    if _TMAP_CACHE["ver"] != ver or _TMAP_CACHE["val"] is None:
        _TMAP_CACHE["val"] = {
            r["rowid"]: r["topic"]
            for r in memory.conn().execute("SELECT rowid, topic FROM knowledge")
        }
        _TMAP_CACHE["ver"] = ver
    return _TMAP_CACHE["val"]


def topic_graph(edge_min: float = 0.32) -> dict:
    """主题语义网络：每个主题一个节点（用其知识点向量的质心表示），

    两主题的质心余弦相似度 >= edge_min 才连边。前端据此做力导向布局。
    质心来自已归一化的向量矩阵（_all_vectors 缓存的即是单位向量），
    所以质心点积就是余弦相似度，O(T²) 且 T 通常 < 40，足够快。
    """
    ver = memory.version()
    if (
        _GRAPH_CACHE["val"] is not None
        and _GRAPH_CACHE["ver"] == ver
        and _GRAPH_CACHE["edge_min"] == edge_min
    ):
        return _GRAPH_CACHE["val"]

    stats = topic_stats()
    if len(stats) < 2:
        result = {"nodes": stats, "edges": []}
        _GRAPH_CACHE.update(ver=ver, edge_min=edge_min, val=result)
        return result
    # 向量维度取决于 embedding 模型（bge-m3=1024，MiniLM=384），从库里读真实值
    dim_row = memory.conn().execute(
        "SELECT length(embedding)/4 dim FROM knowledge WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    if not dim_row or not dim_row["dim"]:
        result = {"nodes": stats, "edges": []}
        _GRAPH_CACHE.update(ver=ver, edge_min=edge_min, val=result)
        return result
    rowids, mat = memory._all_vectors(int(dim_row["dim"]))
    if not rowids or mat.shape[0] == 0:
        result = {"nodes": stats, "edges": []}
        _GRAPH_CACHE.update(ver=ver, edge_min=edge_min, val=result)
        return result
    tmap = _rowid_topic_map()
    # 按主题累加质心（保持「按行遍历顺序决定 topics 次序」的旧语义，保证输出一致）
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
    # 边提取向量化：triu_indices 的 (i,j) 序与旧的双重循环 i<j 完全一致，
    # 先用布尔掩码在 numpy 里筛掉不达标的 pair，再只给通过的 pair 建 dict——
    # 旧实现是 T(T-1)/2 次 Python 迭代（40 个主题 = 780 次 float()/比较/append），
    # 现在只剩通过的几十次。
    # astype(float64) 不能省：旧代码是 Python float（float64）与 edge_min 比较，
    # 直接在 float32 矩阵上比会把阈值降成 float32，阈值边缘的取值会分叉。
    edges = []
    if len(topics) > 1:
        iu = np.triu_indices(len(topics), 1)
        w = sim[iu].astype(np.float64)
        mask = w >= edge_min
        for i, j, wv in zip(iu[0][mask].tolist(), iu[1][mask].tolist(), w[mask].tolist()):
            edges.append({"a": topics[i], "b": topics[j], "w": round(wv, 3)})
    result = {"nodes": stats, "edges": edges}
    _GRAPH_CACHE.update(ver=ver, edge_min=edge_min, val=result)
    return result


def session_timeline(limit: int = 20) -> dict:
    """学习历程：把历次学习任务（sessions）整理成时间线，供「学习历程」卡片画条形。

    每条会话给出：方向、轮次、新增点数、阅读篇数、耗时、单轮效率（点数/轮）。
    这是 EvoMind「自主进化」闭环的直接可视化——看得见「它到底学了几轮、每轮产出多少」。
    """
    rows = memory.list_sessions(limit)
    items = []
    for r in rows:
        started = r.get("started_at") or 0
        ended = r.get("ended_at") or 0
        dur = max(0.0, ended - started) if ended else 0.0
        rounds = int(r.get("rounds") or 0)
        pts = int(r.get("new_points") or 0)
        items.append(
            {
                "id": r.get("id"),
                "direction": r.get("direction", ""),
                "status": r.get("status", ""),
                "rounds": rounds,
                "new_points": pts,
                "fetched": int(r.get("fetched") or 0),
                "duration_s": int(dur),
                "per_round": round(pts / rounds, 2) if rounds > 0 else 0.0,
                "started_at": started,
            }
        )
    # 按开始时间升序：最早的学习会话在最左/最底，符合「历程」直觉
    items.sort(key=lambda x: x["started_at"])
    return {
        "sessions": items,
        "total_sessions": len(items),
        "total_points": sum(x["new_points"] for x in items),
        "total_rounds": sum(x["rounds"] for x in items),
    }


def suggestions(limit: int = 4) -> list[dict]:
    """学习建议（引导）：从知识库现状推导「下一步做什么」。

    返回统一结构的条目 {level, icon, title, detail, view, btn}，前端「下一步建议」
    卡片直接渲染；view/btn 用于跳转（如 learn=去学习）。空库时给新手上路。
    """
    total = memory.count_points()
    items: list[dict] = []

    def add(level: str, icon: str, title: str, detail: str, view: str, btn: str) -> None:
        items.append(
            {"level": level, "icon": icon, "title": title,
             "detail": detail, "view": view, "btn": btn}
        )

    # 规模是性能问题，优先级最高（和前端旧规则保持一致）
    scale_warn = (memory.stats() or {}).get("scale_warn") or ""
    if scale_warn:
        add("warn", "!", "知识库规模提醒", scale_warn, "knowledge", "去整理")

    if total == 0:
        add(
            "act", "◐", "先让它学一个方向",
            "去「自主进化」填一个具体方向，选 3 轮快速摸底。约 10 分钟就有第一批知识，"
            "之后总览和分析页就有东西看了。",
            "learn", "去学习",
        )
        return items[:limit]

    topics = topic_stats()
    growth = growth_by_day(days=30)
    sources = source_breakdown()

    # 1) 久未学习：按最后新增日期算闲置天数
    if growth:
        try:
            last = date.fromisoformat(growth[-1]["day"])
            idle = (date.today() - last).days
        except ValueError:
            idle = 0
        if idle >= 7:
            add(
                "warn", "◔", f"已经 {idle} 天没有新增",
                "学习停滞比学得慢更值得关注。回「自主进化」换个角度，"
                "或就上次的方向再跑几轮，保持知识库的连续性。",
                "learn", "继续学",
            )

    # 2) 薄弱方向：点数明显少于主方向，且不止一个方向时值得补
    if len(topics) >= 2:
        top = topics[0]["count"]
        thin = [t for t in topics[1:] if t["count"] <= max(3, top // 8)]
        if thin:
            names = "、".join(t["topic"] for t in thin[:2])
            add(
                "act", "◑", "有方向还比较薄",
                f"{names} 只有 {thin[0]['count']} 条左右。针对性再学几轮，"
                "知识版图会均衡很多。",
                "learn", "去补盲区",
            )

    # 3) 低可信度主题：学得快但没验证过，提醒回头确认
    low_conf = [t for t in topics if t["avg_conf"] and t["avg_conf"] < 0.5 and t["count"] >= 3]
    if low_conf:
        t = low_conf[0]
        add(
            "warn", "◕", f"「{t['topic']}」可信度偏低",
            f"平均只有 {t['avg_conf']:.2f}。去「检验所学」挑这个方向的问题追问，"
            "答得含糊说明当初的资料不够硬。",
            "chat", "去检验",
        )

    # 4) 资料类型单一：换信源类型比加大剂量更有效
    if sources:
        top_src = sources[0]
        share = top_src["count"] / max(1, total)
        if share >= 0.75 and len(sources) >= 1:
            add(
                "info", "⤢", "资料类型有点单一",
                f"{share * 100:.0f}% 的知识来自{top_src['kind']}。"
                "论文、仓库、问答站的视角不同，混着学更不容易有盲区。",
                "resources", "找找资料",
            )

    # 5) 规模引导：不同阶段给不同的下一步
    if total >= 120:
        add(
            "act", "●", "可以试试权重进化了",
            f"已积累 {total} 条知识。去「权重进化」把它们压进模型参数，"
            "得到一个真正「学会」的版本，再用它回答会明显不一样。",
            "finetune", "去微调",
        )
    elif total >= 30:
        add(
            "act", "◕", "检验一下学得怎么样",
            f"已有 {total} 条知识。去「检验所学」问几个真问题，"
            "答不上来的地方就是下一步该补的方向。",
            "chat", "去提问",
        )
    elif total < 30:
        add(
            "info", "◑", "知识还比较薄",
            f"目前 {total} 条，建议再跑几轮加深，或换个角度补盲区。",
            "learn", "继续学",
        )

    # 6) 微调环境未就绪
    try:
        from . import finetune

        if not finetune.check_env().get("ready"):
            add(
                "info", "⚙", "微调环境还没装",
                "想用权重层进化，需要先装 PyTorch 等依赖（约 3GB，装一次）。",
                "finetune", "去安装",
            )
    except Exception:
        pass

    # 常驻：降低「接下来干嘛」的心智负担
    add(
        "info", "✎", "随时可以问它",
        "它只用学过的知识回答并标出处，不会拿模型记忆糊弄你。",
        "chat", "去问",
    )

    order = {"act": 0, "warn": 1, "info": 2}
    items.sort(key=lambda x: order.get(x["level"], 3))
    return items[:limit]


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


# 版本键缓存 + 安全 TTL：分析视图每 6 秒轮询一次 /api/analytics，旧实现用 3s TTL，
# 永远等不到命中——每次轮询都把整份聚合重算一遍（2000 条约 12ms，随库线性增长，
# 每个打开分析页的标签页每小时白烧 6 秒以上）。
# 这些聚合全是「库内容的确定函数」：库没变过，算出来必然一样。所以以
# memory.version()（每次写入 +1）为键缓存；同时保留 30s 安全 TTL——万一将来
# 新的写入路径漏了 _bump_version()，最多陈旧 30 秒后自愈，不会永久停在旧数据。
_SNAP_CACHE: dict = {"ver": -1, "ts": 0.0, "val": None}
_SNAP_TTL = 30.0


def snapshot(fresh: bool = False) -> dict:
    """一次性返回分析视图所需的全部数据。"""
    now = time.time()
    ver = memory.version()
    if (
        not fresh
        and _SNAP_CACHE["val"] is not None
        and _SNAP_CACHE["ver"] == ver
        and (now - _SNAP_CACHE["ts"]) < _SNAP_TTL
    ):
        return _SNAP_CACHE["val"]
    t0 = time.perf_counter()
    val = {
        "ver": ver,
        "total": memory.count_points(),
        "growth": growth_by_day(),
        "topics": topic_stats(),
        "confidence": confidence_hist(),
        "sources": source_breakdown(),
        "graph": topic_graph(),
        "sessions": session_timeline(),
    }
    _SNAP_CACHE["val"] = val
    _SNAP_CACHE["ts"] = now
    _SNAP_CACHE["ver"] = ver
    with memory._perf_lock:
        memory._perf["snapshot_calls"] += 1
        memory._perf["snapshot_ms"] += (time.perf_counter() - t0) * 1000
    return val


# --------------------------------------------------------------------------
# 知识库健康度（新功能）
# --------------------------------------------------------------------------
def health() -> dict:
    """把分散的统计收成一个总分 + 四个维度的短板，供总览「健康度」卡片使用。

    纯算术：输入全部取自已按版本缓存的 snapshot 组件，不新增任何 SQL，
    轮询时随缓存一起命中。每个维度给出 0-100 分值、一句人话详情；
    weakest 单独拎出来，前端直接拿它做「最该补什么」的引导按钮。
    空库时返回 empty=True，由前端给新手上路引导。
    """
    snap = snapshot()
    total = int(snap.get("total") or 0)
    ver = snap.get("ver", memory.version())
    if total == 0:
        return {
            "ver": ver, "empty": True, "score": 0, "level": "空库",
            "dims": [], "weakest": None,
        }

    topics = snap.get("topics") or []
    conf = snap.get("confidence") or []
    sources = snap.get("sources") or []
    growth = snap.get("growth") or []

    def clamp(x: float) -> int:
        return int(max(0, min(100, round(x))))

    # ① 规模：120 条记为「够用」（再往上不涨分，避免误导用户盲目堆量）
    n_topics = len(topics)
    dims = [{
        "key": "scale", "label": "知识规模", "score": clamp(total / 120 * 100),
        "detail": f"共 {total} 条 · {n_topics} 个方向",
        "hint": "规模还不够，多跑几轮或补新方向" if total < 120 else "规模够用了，重质不重量",
        "view": "learn", "btn": "去学习" if total < 120 else "去补充",
    }]

    # ② 可信度：直方图是精确分桶计数，加权桶心中点即精确平均
    tot_n = sum(b["n"] for b in conf)
    avg_conf = (sum(b["n"] * (b["lo"] + b["hi"]) / 2 for b in conf) / tot_n) if tot_n else 0.0
    dims.append({
        "key": "confidence", "label": "可信度", "score": clamp((avg_conf - 0.3) / 0.5 * 100),
        "detail": f"平均 {avg_conf:.2f}",
        "hint": "可信度偏低，去「检验所学」追问确认" if avg_conf < 0.6 else "可信度健康",
        "view": "chat", "btn": "去检验" if avg_conf < 0.6 else "去提问",
    })

    # ③ 信源多样性：种类数（4 类拿满 60 分）+ 集中度（最高占比越低越多 40 分）
    n_src = len(sources)
    top_src = sources[0] if sources else None
    top_share = (top_src["count"] / total) if top_src else 1.0
    dims.append({
        "key": "sources", "label": "信源多样性", "score": clamp((n_src - 1) / 3 * 60 + (1 - top_share) * 40),
        "detail": f"{n_src} 类信源" + (f" · {top_src['kind']} 占 {top_share * 100:.0f}%" if top_src else ""),
        "hint": "信源太单一，混着学不容易有盲区" if top_share >= 0.75 or n_src < 2 else "信源足够多样",
        "view": "resources", "btn": "找找资料",
    })

    # ④ 新鲜度：最后学习距今天数（7 天内满分，30 天归零）+ 近 7 天增量加成
    idle, last7 = 999, 0
    if growth:
        try:
            last = date.fromisoformat(growth[-1]["day"])
            idle = (date.today() - last).days
            week_ago = (date.today() - timedelta(days=6)).isoformat()
            last7 = sum(g["added"] for g in growth if g["day"] >= week_ago)
        except ValueError:
            idle = 0
    fresh_score = max(0.0, 100 - max(0, idle) * 100 / 30) + min(10, last7)
    dims.append({
        "key": "freshness", "label": "学习新鲜度", "score": clamp(fresh_score),
        "detail": (f"最后学习 {idle} 天前 · 近 7 天 +{last7} 条") if growth else "还没有学习记录",
        "hint": "已经超过一周没学了，保持连续性" if idle >= 7 else "学习节奏正常",
        "view": "learn", "btn": "继续学",
    })

    score = clamp(sum(d["score"] for d in dims) / len(dims))
    level = "待补强" if score < 40 else ("起步" if score < 60 else ("良好" if score < 80 else "优秀"))
    weakest = min(dims, key=lambda d: d["score"])
    return {
        "ver": ver, "empty": False, "score": score, "level": level,
        "dims": dims, "weakest": weakest,
    }


def _reset_caches() -> None:
    """清空全部版本键缓存。

    生产进程内知识库不会被整体换掉，无需调用；测试套件每例都换一个临时库，
    必须在换库后清一次，否则上一例的缓存会带着旧版本号误命中。
    """
    _SNAP_CACHE.update(ver=-1, ts=0.0, val=None)
    _GRAPH_CACHE.update(ver=-1, edge_min=None, val=None)
    _TMAP_CACHE.update(ver=-1, val=None)
