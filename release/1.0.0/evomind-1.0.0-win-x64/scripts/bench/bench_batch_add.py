"""批量入库 vs 逐条入库 的 A/B 基准（纯 DB/去重开销，隔离 LLM）。

用法：
  bench_batch_add.py [轮数] [库内已有条数] [每轮插入条数]

只预载一次基线库，之后逐轮交替「逐条 add_point」与「add_points_batch」，
各插入一批全新互不重复的点。两种方法面对同样增长的库，对比才公平，
且省掉每轮重建基线的开销。
"""
from __future__ import annotations
import sys, time, statistics
sys.path.insert(0, "scripts/bench")
import _env

_env.isolate()
_env.fake_llm()

from backend import memory

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
EXIST = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
ADD = int(sys.argv[3]) if len(sys.argv) > 3 else 200


def seed():
    c = memory.conn()
    c.execute("DELETE FROM knowledge")
    c.commit()
    memory._invalidate()
    for i in range(EXIST):
        memory.add_point(
            topic="T",
            claim=f"基线种子知识点 {i} 用于构建已有库避免重复插入的填充内容句。",
            detail="填充" * 80,
            dedup=False,
        )
    memory._invalidate()


def run_single(round_idx: int) -> float:
    t = time.perf_counter()
    for i in range(ADD):
        memory.add_point(
            topic="T",
            claim=f"逐条新增知识点 R{round_idx}-{i} 内容各不相同互不重复的压测句子用于去重矩阵乘。",
            detail="内容" * 80,
            dedup=True,
        )
    return (time.perf_counter() - t) * 1000


def run_batch(round_idx: int) -> float:
    pts = [
        {
            "topic": "T",
            "claim": f"批量新增知识点 R{round_idx}-{i} 内容各不相同互不重复的压测句子用于去重矩阵乘。",
            "detail": "内容" * 80,
            "vec": None,
        }
        for i in range(ADD)
    ]
    t = time.perf_counter()
    memory.add_points_batch(pts, dedup=True)
    return (time.perf_counter() - t) * 1000


seed()
single, batch = [], []
for r in range(ROUNDS):
    if r % 2 == 0:
        single.append(run_single(r))
        batch.append(run_batch(r))
    else:
        batch.append(run_batch(r))
        single.append(run_single(r))

med_s = statistics.median(single)
med_b = statistics.median(batch)
print(f"轮数={ROUNDS} 库内起始={EXIST} 每轮插入={ADD}（库随轮增长）")
print(f"逐条 add_point : 中位 {med_s:8.1f} ms  区间 [{min(single):.1f}, {max(single):.1f}]")
print(f"批量 add_points: 中位 {med_b:8.1f} ms  区间 [{min(batch):.1f}, {max(batch):.1f}]")
print(f"提速 {med_s / med_b:.2f}x")
pairs = list(zip(single, batch))
wins = sum(1 for s, b in pairs if b < s)
print(f"批量更快的轮数: {wins}/{ROUNDS}")
