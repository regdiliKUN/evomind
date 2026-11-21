"""Probe: analytics.snapshot 聚合 —— 旧(5 次全表 GROUP BY) vs 新(单遍读全列)。

只探测信号强度，不污染 analytics.py。两边都基于 memory 真实库状态。
"""
import sys, time, random, math
sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")
import _env; _env.isolate(); _env.fake_llm()
from backend import memory, analytics


def seed(n=4000):
    now = time.time()
    for i in range(n):
        memory.add_point(
            topic=f"方向{random.randint(0, 11)}",
            claim=f"知识点 {random.random()} 用于探测快照单遍聚合相比多次全表扫描是否足够显著的内容。",
            detail="d " * 15,
            source_kind=random.choice(["web", "book", "web", "paper", "web"]),
            confidence=round(random.random(), 3),
            dedup=False,
        )


def single_pass(days=60, bins=10):
    rows = memory.conn().execute(
        "SELECT rowid, topic, confidence, source_kind, created_at FROM knowledge"
    ).fetchall()
    # growth
    per_day = {}
    for r in rows:
        d = time.strftime("%Y-%m-%d", time.gmtime(r["created_at"] or 0))
        per_day[d] = per_day.get(d, 0) + 1
    total = len(rows)
    ds = sorted(per_day.keys(), reverse=True)[:days]
    cum = total
    growth = []
    for d in reversed(ds):
        cum -= per_day[d]
        growth.append({"day": d, "added": per_day[d], "cumulative": cum + per_day[d]})
    growth.reverse()
    # topics
    tcnt, tsum = {}, {}
    for r in rows:
        t = r["topic"]
        tcnt[t] = tcnt.get(t, 0) + 1
        tsum[t] = tsum.get(t, 0.0) + (r["confidence"] or 0.0)
    topics = sorted(
        ({"topic": t, "count": tcnt[t], "avg_conf": round(tsum[t] / tcnt[t], 3)} for t in tcnt),
        key=lambda x: (-x["count"], x["topic"]),
    )[:40]
    # confidence hist
    b = {}
    for r in rows:
        i = int(min(0.9999, r["confidence"] or 0.0) * bins)
        b[i] = b.get(i, 0) + 1
    conf = [{"lo": round(i / bins, 2), "hi": round((i + 1) / bins, 2), "n": b.get(i, 0)} for i in range(bins)]
    # sources
    sc = {}
    for r in rows:
        k = r["source_kind"] if r["source_kind"] else "未知"
        sc[k] = sc.get(k, 0) + 1
    sources = sorted(({"kind": k, "count": v} for k, v in sc.items()), key=lambda x: (-x["count"], x["kind"]))
    return {"growth": growth, "topics": topics, "confidence": conf, "sources": sources}


def norm(d):
    return {
        "growth": [(g["day"], g["added"], g["cumulative"]) for g in d["growth"]],
        "topics": sorted((t["topic"], t["count"], t["avg_conf"]) for t in d["topics"]),
        "confidence": [(c["lo"], c["hi"], c["n"]) for c in d["confidence"]],
        "sources": sorted((s["kind"], s["count"]) for s in d["sources"]),
    }


def main():
    seed()
    memory._all_vectors(1024)  # 预热向量缓存，隔离出「轻量聚合」的差异
    ROUNDS = 24
    ts_old, ts_new = [], []
    mism = 0
    for r in range(ROUNDS):
        t0 = time.perf_counter()
        old = analytics.snapshot(fresh=True)
        to = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        new = single_pass()
        tn = (time.perf_counter() - t0) * 1000
        ts_old.append(to); ts_new.append(tn)
        if norm(old) != norm(new):
            mism += 1
            if mism == 1:
                for kk in norm(old):
                    if norm(old)[kk] != norm(new)[kk]:
                        print("字段不一致:", kk)
    med_o = sorted(ts_old)[ROUNDS // 2]
    med_n = sorted(ts_new)[ROUNDS // 2]
    wins = sum(1 for o, n in zip(ts_old, ts_new) if n < o)
    print(f"旧(5次GROUP BY)中位 {med_o:.2f}ms | 新(单遍)中位 {med_n:.2f}ms | 加速 {med_o/med_n:.2f}x | 更快 {wins}/{ROUNDS}")
    print("聚合字段不一致轮次:", mism)


if __name__ == "__main__":
    main()
