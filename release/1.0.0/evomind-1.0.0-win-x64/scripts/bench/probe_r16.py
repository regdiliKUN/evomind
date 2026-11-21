"""探针（第十六轮）：把书库这一块按「真实体量」压一遍，用数据选靶。

前几轮把知识库/分析/事件三条线都扫平了，但书库一直只按 1 个目录、300 本书
来过。真实用户是「十几个收藏目录 + 几千本书 + 几千条知识点」，
`list_dirs()` 的每目录两条 COUNT、`list_books()` 的「同一条 SQL 跑两遍」
在这种体量下会不会真的疼，得测了才知道。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()
_env.fake_llm()

import numpy as np  # noqa: E402

from backend import analytics, library, memory  # noqa: E402
from backend import main as api  # noqa: E402

# ---- 造体量 ----------------------------------------------------------------
N_POINTS = 2000
DIM = 1024
rng = np.random.default_rng(17)
for i in range(N_POINTS):
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

library.init_db()
c = memory.conn()

# 30 个收藏目录（真实用户翻几年书的量级）
for d in range(30):
    c.execute(
        "INSERT OR REPLACE INTO library_dirs(id,path,label,enabled,excludes,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"dir{d:02d}", f"D:/books/收藏_{d:02d}", f"收藏 {d:02d}", 1, "[]",
         time.time() - d * 86400),
    )
# 3000 本书，均匀摊到 30 个目录
N_BOOKS = 3000
for i in range(N_BOOKS):
    c.execute(
        "INSERT OR REPLACE INTO books(id,path,title,format,size_bytes,chars,"
        "pages,status,points_count,dir_id,source_type,created_at,topic) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"b{i:04d}", f"D:/books/收藏_{i % 30:02d}/book{i}.pdf",
         f"深度学习实践 第 {i} 卷", "pdf", 3_000_000, 320_000, 240,
         "ready" if i % 3 else "done", i % 7, f"dir{i % 30:02d}", "dir",
         time.time() - i * 3600, "方向_01"),
    )
c.commit()
analytics._reset_caches()

BID = "b0001"
PID = memory.list_points(limit=1)[0]["id"]

CASES = [
    ("/api/library/dirs", lambda: api.api_lib_dirs()),
    ("/api/library/stats", lambda: api.api_lib_stats()),
    ("/api/library/books (200)", lambda: api.api_lib_books(limit=200)),
    ("/api/library/books?q", lambda: api.api_lib_books(q="深度学习", limit=200)),
    ("/api/library/books?status", lambda: api.api_lib_books(status="ready", limit=200)),
    ("/api/library/books?dir", lambda: api.api_lib_books(dir_id="dir07", limit=200)),
    ("/api/library/books/{id}", lambda: api.api_lib_book(BID)),
    ("/api/library/state", lambda: api.api_lib_state()),
    ("/api/status", lambda: api.api_status()),
    ("/api/knowledge (60)", lambda: api.api_knowledge(limit=60)),
    ("/api/knowledge?q", lambda: api.api_knowledge(q="知识点 40 字", limit=60)),
    ("/api/knowledge/{pid}", lambda: api.api_knowledge_detail(PID, k=6)),
    ("/api/analytics", lambda: api.api_analytics()),
    ("/api/ocr/state", lambda: api.api_ocr_state()),
    ("/api/finetune/state", lambda: api.api_ft_state()),
]

print(f"seed：{N_POINTS} 知识点 / 30 目录 / {N_BOOKS} 本书\n")
rows = []
for name, fn in CASES:
    fn()  # 预热
    iters = 20
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    ms = (time.perf_counter() - t0) / iters * 1000
    rows.append((ms, name))

rows.sort(reverse=True)
print(f"{'端点':<28}{'单次':>12}")
print("-" * 44)
for ms, name in rows:
    print(f"{name:<28}{ms:>10.3f} ms")

# ---- 直接确认 N+1 的往返次数 ----------------------------------------------
import sqlite3  # noqa: E402


def count_sql(fn):
    """用 sqlite 的 trace 回调精确统计一次调用发出多少条语句。"""
    seen: list[str] = []
    conn = memory.conn()
    conn.set_trace_callback(lambda s: seen.append(s))
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return len(seen), seen


print()
n_dirs, _ = count_sql(lambda: None)
print("单次调用发出的 SQL 条数：")
for label, fn in [
    ("/api/library/dirs", api.api_lib_dirs),
    ("/api/library/books?q", lambda: api.api_lib_books(q="深度学习", limit=200)),
    ("/api/library/books (200)", lambda: api.api_lib_books(limit=200)),
]:
    n, stmts = count_sql(fn)
    print(f"  {label:<24} {n:>3} 条")
    for s in stmts[:3]:
        print("      ·", " ".join(s.split())[:90])
    if n > 3:
        print(f"      · …（其余 {n - 3} 条为循环内重复语句）")
