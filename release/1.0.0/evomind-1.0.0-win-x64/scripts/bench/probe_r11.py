"""探针：在真实规模（2000 条 / 1024 维）下测量各热点路径单次成本。

目的：给第十一轮选瓶颈提供数据，而不是凭感觉。测量对象：
  1. memory.stats()              —— /api/status 每次轮询都跑
  2. memory.list_points(60)      —— /api/knowledge 知识库视图每次轮询都拉
  3. analytics.snapshot(fresh)   —— 分析视图聚合全量重算（TTL 3s，6s 轮询等于每两次重算一次）
  4. main.api_status() 组合       —— 轮询端到端（stub Ollama）
  5. 前端等价物：60 行 innerHTML 串构建（Node 侧另测）
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()

import numpy as np  # noqa: E402

from backend import analytics, config, memory  # noqa: E402
from backend.main import api_status  # noqa: E402

N = 2000
DIM = 1024

# ---- 造库：2000 条、40 个方向、近似真实的文本长度 ----
rng = np.random.default_rng(7)
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

memory._bump_search_cache()
analytics._SNAP_CACHE["val"] = None


def bench(name: str, fn, rounds: int = 15) -> float:
    fn()  # 预热
    ts = []
    for _ in range(rounds):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    print(f"{name:34s} 中位 {ts[len(ts) // 2]:8.2f} ms   最小 {ts[0]:8.2f} ms")
    return ts[len(ts) // 2]


r = {}
r["stats"] = bench("memory.stats()", memory.stats)
r["list60"] = bench("memory.list_points(limit=60)", lambda: memory.list_points(None, 60, 0))
r["snap"] = bench("analytics.snapshot(fresh=True)", lambda: analytics.snapshot(fresh=True))
r["status"] = bench("main.api_status() 组合", api_status)

# 6 秒轮询一次的小时级成本
per_hour = {k: v * 600 for k, v in r.items()}
print("\n每个打开着的标签页每小时（600 次轮询）的重复计算：")
for k, v in per_hour.items():
    print(f"  {k:10s} {v / 1000:8.2f} s")
