"""等价性核对：同一本书分别走「逐条 add_point」与「批量 add_points_batch」，
比对最终入库的 claim 集合是否一致。

两条路径用同一个 fake-LLM（确定性向量），所以差异只能来自批量去重的实现。
用法：
  cmp_import.py <fixture名>            # 默认 text_book.pdf
  cmp_import.py <fixture名> peritem    # 只生成逐条版
  cmp_import.py <fixture名> batch      # 只生成批量版
"""
from __future__ import annotations
import sys, hashlib, json
from pathlib import Path

sys.path.insert(0, "scripts/bench")
import _env

_env.isolate()
_env.fake_llm()

from backend import library, memory

NAME = sys.argv[1] if len(sys.argv) > 1 else "text_book.pdf"
MODE = sys.argv[2] if len(sys.argv) > 2 else "both"
OUT = _env.BENCH_DIR / "db"
PERITEM = OUT / "claims_peritem.json"
BATCH = OUT / "claims_batch.json"


def import_one(use_batch: bool) -> list[str]:
    # 把批量入口动态换成逐条，或保持批量
    real_batch = memory.add_points_batch

    def per_item(cands, **kw):
        added = 0
        accepted = []
        for c in cands:
            pid = memory.add_point(
                topic=c.get("topic", ""),
                claim=c["claim"],
                detail=c.get("detail", ""),
                source_url=c.get("source_url", ""),
                source_title=c.get("source_title", ""),
                source_kind=c.get("source_kind", "web"),
                tags=c.get("tags") or [],
                confidence=c.get("confidence", 0.6),
                session_id=kw.get("session_id", ""),
                book_id=kw.get("book_id", ""),
                vec=c.get("vec"),
                dedup=True,
            )
            if pid:
                added += 1
                accepted.append(c["claim"])
        return {"added": added, "dup": 0, "skipped": 0, "accepted": accepted}

    if not use_batch:
        memory.add_points_batch = per_item  # type: ignore
    try:
        p = _env.FIXTURES / NAME
        library.init_db()
        c = memory.conn()
        c.execute("DELETE FROM knowledge")
        c.commit()
        memory._invalidate()
        bid = hashlib.md5(str(p).encode("utf-8")).hexdigest()[:16]
        c.execute(
            "INSERT OR REPLACE INTO books(id,dir_id,path,source_type,format,title,"
            "size_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, "", str(p), "local", p.suffix.lstrip("."), p.stem,
             p.stat().st_size, "pending", 0.0),
        )
        c.commit()
        library.parse_book(bid)
        imp = library.import_book(bid, topic="等价性核对", max_chunks=40)
        rows = c.execute("SELECT claim FROM knowledge ORDER BY claim").fetchall()
        claims = [r["claim"] for r in rows]
        print(f"[{'batch' if use_batch else 'peritem'}] {NAME}: 接受 {len(claims)} 条, "
              f"import_book.points={imp.get('points')}")
        return claims
    finally:
        memory.add_points_batch = real_batch  # type: ignore


if MODE in ("peritem", "both"):
    a = import_one(use_batch=False)
    PERITEM.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")

if MODE in ("batch", "both"):
    b = import_one(use_batch=True)
    BATCH.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")

if MODE == "both":
    sa, sb = set(a), set(b)
    print("\n==== 比对 ====")
    print(f"逐条集合大小 {len(sa)}，批量集合大小 {len(sb)}")
    print(f"逐条独有（批量误删）: {len(sa - sb)}")
    print(f"批量独有（逐条漏掉）: {len(sb - sa)}")
    if sa - sb:
        for x in sorted(sa - sb)[:20]:
            print("  误删 |", x[:80])
    if sb - sa:
        for x in sorted(sb - sa)[:20]:
            print("  漏加 |", x[:80])
    print("结论:", "完全一致 ✅" if sa == sb else "存在差集 ⚠️")
