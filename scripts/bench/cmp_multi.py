"""多书等价性核对：把若干 fixture 依次导入，分别走「逐条 add_point」与
「批量 add_points_batch」，比对最终整库 claim 集合是否一致。

比单书更严格：能复现「跨书去重 / 同一批内去重」两种场景。
用法：cmp_multi.py [fixture1 fixture2 ...]   （默认一组常见格式）
"""
from __future__ import annotations
import sys, hashlib, json
from pathlib import Path

sys.path.insert(0, "scripts/bench")
import _env

_env.isolate()
_env.fake_llm()

from backend import library, memory

FIXTURES = sys.argv[1:] or ["plain.txt", "book.docx", "book.epub", "gbk_book.txt", "big.txt"]
OUT = _env.BENCH_DIR / "db"
PERITEM = OUT / "multi_peritem.json"
BATCH = OUT / "multi_batch.json"


def run(use_batch: bool) -> list[str]:
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
        library.init_db()
        c = memory.conn()
        c.execute("DELETE FROM knowledge")
        c.commit()
        memory._invalidate()
        total = 0
        for name in FIXTURES:
            p = _env.FIXTURES / name
            if not p.exists():
                print("跳过的缺失 fixture:", name)
                continue
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
            total += imp.get("points", 0)
        rows = c.execute("SELECT claim FROM knowledge ORDER BY claim").fetchall()
        claims = [r["claim"] for r in rows]
        print(f"[{'batch' if use_batch else 'peritem'}] 共导入 {len(FIXTURES)} 本, "
              f"import_book 累计 {total} 条, 实际入库 {len(claims)} 条")
        return claims
    finally:
        memory.add_points_batch = real_batch  # type: ignore


a = run(use_batch=False)
PERITEM.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")
b = run(use_batch=True)
BATCH.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")

sa, sb = set(a), set(b)
print("\n==== 多书比对 ====")
print(f"逐条集合 {len(sa)}，批量集合 {len(sb)}")
print(f"逐条独有（批量误删）: {len(sa - sb)}")
print(f"批量独有（逐条漏加）: {len(sb - sa)}")
for x in sorted(sa - sb)[:25]:
    print("  误删 |", x[:80])
for x in sorted(sb - sa)[:25]:
    print("  漏加 |", x[:80])
print("结论:", "完全一致 ✅" if sa == sb else "存在差集 ⚠️")
