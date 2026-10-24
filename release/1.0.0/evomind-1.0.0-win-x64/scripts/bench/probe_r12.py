"""探针：第十二轮瓶颈定位。

在真实规模（2000 条 / 1024 维 / 40 方向）下测量前几轮没碰过的路径：
  1. memory.keyword_search()   —— 问答兜底路径，SELECT * 会拖出 embedding blob
  2. memory.log_event()        —— 学习过程中每条事件一次独立 commit
  3. memory.recent_events(30)  —— 前端 /api/events/recent 每次刷新都拉
  4. memory.search() 空结果   —— 兜底触发条件（空库主题检索）
  5. 对照：memory.search() 命中（已优化过，作为尺子）
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

from backend import analytics, memory  # noqa: E402

N = 2000
DIM = 1024

rng = np.random.default_rng(11)
t0 = time.perf_counter()
for i in range(N):
    topic = f"方向_{(i % 40):02d} · 某个具体的学习方向名称"
    claim = f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。"
    detail = "细节正文。" * 60
    v = rng.standard_normal(DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    memory.add_point(
        topic=topic, claim=claim, detail=detail,
        source_url=f"https://example.com/{i}", source_title=f"资料 {i}",
        source_kind="web", tags=["a", "b", "c"], confidence=0.6,
        vec=v.tolist(), dedup=False,
    )
print(f"seed {N} 条耗时 {time.perf_counter() - t0:.1f}s")
print(f"库体积: {memory.conn().execute('SELECT page_count*page_size FROM pragma_page_count(), pragma_page_size()').fetchone()[0] / 1e6:.1f} MB")

memory._bump_search_cache()
analytics._reset_caches()


def bench(name: str, fn, rounds: int = 15) -> float:
    fn()
    ts = []
    for _ in range(rounds):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    med = ts[len(ts) // 2]
    print(f"{name:38s} 中位 {med:8.3f} ms   最小 {ts[0]:8.3f} ms")
    return med


print("\n---- 单次成本 ----")
bench("keyword_search('知识点', k=6)", lambda: memory.keyword_search("知识点", k=6))
bench("keyword_search('不存在的词', k=6)", lambda: memory.keyword_search("zzz_nohit", k=6))
bench("recent_events(30)", lambda: memory.recent_events(30))
bench("recent_events(1000)", lambda: memory.recent_events(1000))
bench("search('随便什么问题', k=8)", lambda: memory.search("随便什么问题", k=8))


def log_one():
    memory.log_event("stage", "检索到 12 条候选，准备提炼")


print(f"\n---- log_event 单条 commit ----")
bench("log_event x1", log_one, rounds=40)

print("\n---- 学习一轮的事件量级 ----")
# 一轮大致 8~15 条事件；模拟 12 条
def log_twelve():
    for i in range(12):
        memory.log_event("stage", f"模拟第 {i} 条学习过程事件", {"round": 1})


bench("log_event x12（一轮量级）", log_twelve, rounds=20)

# 批量化收益测量：同样 12 条,一个事务
def log_twelve_batched():
    c = memory.conn()
    with c:
        for i in range(12):
            c.execute(
                "INSERT INTO events(ts,kind,message,payload) VALUES (?,?,?,?)",
                (time.time(), "stage", f"批量第 {i} 条", "{}"),
            )


bench("12 条单事务（上界参考）", log_twelve_batched, rounds=20)

_rows = memory.conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
_ev = memory.conn().execute("SELECT SUM(pgsize) FROM dbstat WHERE name='events'").fetchone()[0] or 0
_kb = memory.conn().execute("SELECT SUM(pgsize) FROM dbstat WHERE name='knowledge'").fetchone()[0] or 0
print(f"\n事件表总行数: {_rows}")
print(f"events 表体积: {_ev / 1e6:.2f} MB")
print(f"knowledge 表体积: {_kb / 1e6:.2f} MB")
