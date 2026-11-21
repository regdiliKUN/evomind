"""探针：用 cProfile 对「前端一次完整轮询」做组合剖析，找剩下的真热点。

前端每 6 秒依次打：/api/status → /api/analytics/suggestions →
（当前视图）dashboard: /api/events/recent + /api/knowledge，或 analytics: /api/analytics + /api/analytics/health。
这里在 2000 条规模下按真实顺序跑一遍，用 cProfile 按 tottime 排序，避免凭感觉选靶。
"""
from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()
_env.fake_llm()

import numpy as np  # noqa: E402

from backend import analytics, memory  # noqa: E402
from backend.main import (  # noqa: E402
    api_analytics, api_analytics_health, api_analytics_suggestions,
    api_events_recent, api_knowledge, api_status,
)

N = 2000
DIM = 1024
rng = np.random.default_rng(13)
for i in range(N):
    topic = f"方向_{(i % 40):02d} · 某个具体的学习方向名称"
    v = rng.standard_normal(DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    memory.add_point(
        topic=topic,
        claim=f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。",
        detail="细节正文。" * 60,
        source_url=f"https://example.com/{i}", source_title=f"资料 {i}",
        source_kind="web", tags=["a", "b", "c"], confidence=0.6,
        vec=v.tolist(), dedup=False,
    )
for i in range(400):
    memory.log_event("stage", f"历史事件 {i}", {"round": i % 12})
memory._bump_search_cache()
analytics._reset_caches()
print(f"seed 完成：{N} 条知识点 / 400 条事件")


def one_poll_dashboard():
    api_status()
    api_analytics_suggestions()
    api_events_recent(limit=30)
    api_knowledge(limit=60)


def one_poll_analytics():
    api_status()
    api_analytics_suggestions()
    api_analytics()
    api_analytics_health()


# 预热（首次建向量缓存、snapshot 首算）
one_poll_dashboard()
one_poll_analytics()
analytics._reset_caches()  # 让剖析里包含一次真实的 snapshot 冷算


for name, fn, times in (("dashboard 轮询", one_poll_dashboard, 20),
                        ("analytics 轮询", one_poll_analytics, 20)):
    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    for _ in range(times):
        fn()
    pr.disable()
    wall = (time.perf_counter() - t0) / times * 1000
    print(f"\n==== {name}：每次 {wall:.2f} ms ====")
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(14)
    lines = s.getvalue().splitlines()
    started = False
    shown = 0
    for ln in lines:
        if "ncalls" in ln:
            started = True
        if started:
            print(ln)
            shown += 1
            if shown > 17:
                break
