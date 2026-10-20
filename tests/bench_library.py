"""第十六轮 A/B：书库读路径的两处去冗余，24 轮交替对照 + 逐字段等价。

  A. library.init_db()   —— 每次请求都重跑 7 条 DDL + 1 次必然失败的 ALTER
                            + 1 次建索引 + commit（挂在 8 个读路径上）
  B. library.list_dirs() —— 每个目录两条 COUNT（N+1），30 目录 = 61 条语句

（另一处 list_books() 的「COUNT + 分页」双跑，试过用窗口函数
  COUNT(*) OVER () 合并成一趟，实测慢 3 倍多，已回退，见报告。）
"""
from __future__ import annotations

import json
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

ROUNDS = 24
N_DIRS = 30
N_BOOKS = 3000

rng = np.random.default_rng(17)
for i in range(400):
    v = rng.standard_normal(1024).astype(np.float32)
    v /= np.linalg.norm(v)
    memory.add_point(
        topic=f"方向_{(i % 40):02d} · 某个具体的学习方向名称",
        claim=f"这是第 {i} 条知识点，claim 长度约 40 字。",
        detail="细节正文。" * 60,
        source_url=f"https://example.com/{i}", source_title=f"资料 {i}",
        source_kind="web", tags=["a"], confidence=0.6, vec=v.tolist(), dedup=False,
    )

library.init_db()
c = memory.conn()
for d in range(N_DIRS):
    c.execute(
        "INSERT OR REPLACE INTO library_dirs(id,path,label,enabled,excludes,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"dir{d:02d}", f"D:/books/收藏_{d:02d}", f"收藏 {d:02d}", 1,
         '["*.tmp"]' if d % 5 == 0 else "[]", time.time() - d * 86400),
    )
for i in range(N_BOOKS):
    c.execute(
        "INSERT OR REPLACE INTO books(id,path,title,format,size_bytes,chars,"
        "pages,status,points_count,dir_id,source_type,created_at,topic) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"b{i:04d}", f"D:/books/收藏_{i % N_DIRS:02d}/book{i}.pdf",
         f"深度学习实践 第 {i} 卷", "pdf", 3_000_000, 320_000, 240,
         "ready" if i % 3 else "done", i % 7, f"dir{i % N_DIRS:02d}", "dir",
         time.time() - i * 3600, "方向_01"),
    )
c.commit()
analytics._reset_caches()

NEW = dict(_DISABLE_INIT_GUARD=False, _DISABLE_DIRS_TALLY=False)
OLD = dict(_DISABLE_INIT_GUARD=True, _DISABLE_DIRS_TALLY=True)


def flip(**kw):
    for k, v in kw.items():
        setattr(library, k, v)


def timed(fn):
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000


def sql_count(fn):
    seen: list[str] = []
    conn = memory.conn()
    conn.set_trace_callback(lambda s: seen.append(s))
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return len(seen)


# ---------------------------------------------------------------- 等价性 --
print("== 等价性校验 ==")
flip(**NEW)
d_new = json.dumps(library.list_dirs(), ensure_ascii=False, sort_keys=True)
s_new = api.api_lib_stats()
flip(**OLD)
d_old = json.dumps(library.list_dirs(), ensure_ascii=False, sort_keys=True)
s_old = api.api_lib_stats()
flip(**NEW)

eq = {
    "list_dirs 逐字段一致": d_new == d_old,
    "list_dirs 条数正确": len(json.loads(d_new)) == N_DIRS,
    "每个目录 book_count 求和 == 总书数":
        sum(d["book_count"] for d in json.loads(d_new)) == N_BOOKS,
    "每个目录 imported_count 求和 == done 书数":
        sum(d["imported_count"] for d in json.loads(d_new))
        == sum(1 for i in range(N_BOOKS) if i % 3 == 0),
    "excludes 仍解析成数组":
        all(isinstance(d["excludes"], list) for d in json.loads(d_new)),
    "library_stats 逐字段一致": s_new == s_old,
    "目录总数正确": s_new["stats"]["dirs"] == N_DIRS,
    "书目总数正确": s_new["stats"]["books"] == N_BOOKS,
}
for k, v in eq.items():
    print(f"  {'PASS' if v else 'FAIL'}  {k}")
if not all(eq.values()):
    raise SystemExit("等价性不通过，终止")

print()
print(f"== 语句条数（{N_DIRS} 目录 / {N_BOOKS} 本书）==")
flip(**OLD)
old_n = {
    "init_db()": sql_count(library.init_db),
    "/api/library/dirs": sql_count(api.api_lib_dirs),
}
flip(**NEW)
new_n = {
    "init_db()": sql_count(library.init_db),
    "/api/library/dirs": sql_count(api.api_lib_dirs),
}
for k in old_n:
    print(f"  {k:<20} {old_n[k]:>3} 条 → {new_n[k]:>3} 条")

# ---------------------------------------------------------------- 计时 --
CASES = [
    ("init_db()", library.init_db),
    ("/api/library/dirs（含 init_db）", api.api_lib_dirs),
    ("/api/library/stats（含 init_db）", api.api_lib_stats),
    ("/api/library/books（含 init_db）", lambda: api.api_lib_books(q="深度学习", limit=200)),
]

print()
print(f"== {ROUNDS} 轮交替 A/B ==")
print(f"{'场景':<34}{'旧（ms）':>10}{'新（ms）':>10}{'加速':>9}{'新更快':>9}")
print("-" * 74)
summary = {}
for name, fn in CASES:
    fn()
    off_ms, on_ms = [], []
    for r in range(ROUNDS):
        if r % 2 == 0:
            flip(**OLD)
            off_ms.append(timed(fn))
            flip(**NEW)
            on_ms.append(timed(fn))
        else:
            flip(**NEW)
            on_ms.append(timed(fn))
            flip(**OLD)
            off_ms.append(timed(fn))
    flip(**NEW)
    o = sorted(off_ms)[len(off_ms) // 2]
    n = sorted(on_ms)[len(on_ms) // 2]
    wins = sum(1 for a, b in zip(off_ms, on_ms) if b < a)
    speed = o / n if n > 0 else float("inf")
    summary[name] = {
        "old_ms": round(o, 4), "new_ms": round(n, 4),
        "speedup": round(speed, 2), "wins": f"{wins}/{ROUNDS}",
    }
    print(f"{name:<34}{o:>10.4f}{n:>10.4f}{speed:>8.1f}x{wins:>7}/{ROUNDS}")

print()
print(json.dumps({
    "rounds": ROUNDS,
    "scale": {"dirs": N_DIRS, "books": N_BOOKS},
    "sql_before": old_n, "sql_after": new_n,
    "equivalence_passed": sum(1 for v in eq.values() if v),
    "equivalence_total": len(eq),
    "timing": summary,
}, ensure_ascii=False, indent=2))
