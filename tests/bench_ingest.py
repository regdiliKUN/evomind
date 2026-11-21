"""知识摄取写入路径基准：在真实规模库（默认 17426 条）上对比新旧 ingest。

    .venv\\Scripts\\python.exe tests\\bench_ingest.py [--points 30] [--repeat 5]

同一进程、同一份库、同一批向量跑两遍，因此差异只来自实现本身：

  legacy  = 每批把整张 knowledge 表（含 4KB embedding blob）读成 Python 字典，
            再用 Python 循环逐行比较相似度（改动前的实现，此处按原样保留作对照）
  current = 同文查重走 SQL IN、语义查重走 numpy、命中行按 rowid 懒加载

向量用固定种子随机生成，不调用 Ollama，结果可复现。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from backend.knowledge_dedup import conflicts, equivalent, normal, preference, provenance  # noqa: E402


def legacy_ingest(points, vectors, *, session_id="", book_id="", dedup=True):
    """改动前的 ingest：整表 SELECT *，Python 逐行比较。仅作基准对照。"""
    from backend import memory, config

    Q = memory._normalize(np.stack(vectors))
    threshold = float(config.get("dedup_threshold", .93))
    accepted, accepted_rows, duplicates = [], [], 0
    with memory._lock:
        c = memory.conn()
        existing = [dict(r) for r in c.execute("SELECT rowid AS _rid,* FROM knowledge")] if dedup else []
        by_claim = {}
        for p in existing:
            by_claim.setdefault(normal(p["claim"]), []).append(p)
        rids, mat = memory._all_vectors(Q.shape[1])
        by_rid = {p["_rid"]: p for p in existing}
        scores = mat @ Q.T if rids and dedup else np.empty((0, len(points)))
        intra = Q @ Q.T if dedup else None
        order = sorted(range(len(points)), key=lambda i: preference(points[i])) if dedup else range(len(points))
        changed = False
        with c:
            for i in order:
                p = dict(points[i], claim=normal(points[i]["claim"]), detail=normal(points[i].get("detail")),
                         topic=memory._norm_topic(points[i].get("topic", "")), book_id=book_id,
                         session_id=session_id, created_at=time.time())
                target = None
                if dedup:
                    target = next((r for r in by_claim.get(normal(p["claim"]), []) if equivalent(r, p)), None)
                    if target is None:
                        hits = [(float(scores[j, i]), by_rid[rid]) for j, rid in enumerate(rids)
                                if scores[j, i] >= threshold and not conflicts(by_rid[rid], p)]
                        hits += [(float(intra[j, i]), r) for j, r in accepted_rows
                                 if intra[j, i] >= threshold and not conflicts(r, p)]
                        if hits:
                            target = max(hits, key=lambda hit: hit[0])[1]
                if target is not None:
                    duplicates += 1
                    provenance(c, target["id"], target)
                    provenance(c, target["id"], p)
                    continue
                pid = uuid.uuid4().hex[:16]
                p["id"] = pid
                c.execute("INSERT INTO knowledge(id,topic,claim,detail,source_url,source_title,source_kind,"
                          "tags,confidence,embedding,session_id,book_id,created_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (pid, p["topic"], p["claim"], p["detail"], p.get("source_url", ""),
                           p.get("source_title", ""), p.get("source_kind", "web"),
                           json.dumps(list(p.get("tags") or []), ensure_ascii=False),
                           max(.1, min(1., float(p.get("confidence", .6)))),
                           memory._to_blob(vectors[i].tolist()), session_id, book_id, p["created_at"]))
                p["_rid"] = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                accepted.append(p)
                accepted_rows.append((i, p))
                by_claim.setdefault(normal(p["claim"]), []).append(p)
        if accepted or duplicates:
            if changed:
                memory._invalidate()
            else:
                for i, p in accepted_rows:
                    memory._append_vector(p["_rid"], vectors[i].tolist())
            memory._bump_search_cache()
            memory._bump_topic_index()
            memory._bump_version()
    return {"added": len(accepted), "dup": duplicates, "accepted_points": accepted,
            "ids": [p["id"] for p in accepted]}


def make_points(n: int, dim: int, seed: int = 7) -> list[dict]:
    rng = np.random.default_rng(seed)
    pts = []
    for i in range(n):
        v = rng.normal(size=dim).astype(np.float32)
        v /= np.linalg.norm(v)
        pts.append({
            "topic": "bench · 摄取效率",
            "claim": f"基准结论第 {seed}-{i} 条：在固定条件下参数取 {i * 3} 单位时效果最优，偏差不超过 2%。",
            "detail": "基准补充说明，用于模拟真实知识点的 detail 长度。" * 2,
            "source_url": f"https://example.com/bench/{seed}/{i}",
            "source_title": f"bench {seed}",
            "source_kind": "web",
            "tags": ["bench"],
            "confidence": 0.7,
            "vec": v.tolist(),
        })
    return pts


def run(workdir: Path, source_db: Path, n_points: int, repeat: int, legacy: bool) -> dict:
    db = workdir / ("legacy.db" if legacy else "current.db")
    if not source_db.exists():
        raise SystemExit(f"找不到基准库：{source_db}")
    shutil.copy(source_db, db)

    from backend import config as cfg

    cfg.DB_FILE = db
    from backend import llm, memory

    dim = 1024
    probe = np.random.default_rng(1).normal(size=dim).astype(np.float32)
    probe /= np.linalg.norm(probe)
    llm.embed = lambda texts, **kw: [probe.tolist() for _ in texts]  # type: ignore[assignment]
    memory._all_vectors(dim)  # 预热：真实运行里向量缓存是热的，不计入写入耗时

    if legacy:
        def fn(pts, session_id=""):
            vecs = [np.asarray(p["vec"], dtype=np.float32) for p in pts]
            return legacy_ingest(pts, vecs, session_id=session_id)
    else:
        fn = memory.add_points_batch
    samples = []
    for k in range(repeat):
        pts = make_points(n_points, dim, seed=(900 if legacy else 100) + k)
        t0 = time.perf_counter()
        fn(pts, session_id="bench")
        samples.append(time.perf_counter() - t0)
    return {
        "mode": "legacy" if legacy else "current",
        "n_points": n_points,
        "samples_ms": [round(s * 1000, 1) for s in samples],
        "median_ms": round(statistics.median(samples) * 1000, 1),
        "per_point_ms": round(statistics.median(samples) * 1000 / n_points, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=30)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--db", default=str(ROOT / "data" / "knowledge" / "knowledge.db"))
    ap.add_argument("--legacy", action="store_true", help="只跑改动前的实现")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="evomind-bench-"))
    try:
        out = run(tmp, Path(args.db), args.points, args.repeat, args.legacy)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"[{out['mode']}] 批量 {out['n_points']} 条：中位 {out['median_ms']} ms"
              f"（{out['per_point_ms']} ms/条），样本 {out['samples_ms']}")


if __name__ == "__main__":
    main()
