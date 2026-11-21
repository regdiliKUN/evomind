"""探针（第十三轮）：把全部读端点按单次成本排一遍，用数据选靶。

前两轮已经修掉了 snapshot 全量重算与 suggestions 重算聚合。这里覆盖
「用户能点到的每一条只读路径」，包括轮询之外的：书库列表/统计、资源页、
配置页、模型页、知识点详情、按关键词筛知识库等。
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

from backend import analytics, config, library, memory  # noqa: E402
from backend import main as api  # noqa: E402

N = 2000
DIM = 1024
rng = np.random.default_rng(17)
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

# 造一批书目，让书库列表 / 统计有真实体量
library.init_db()
for i in range(300):
    memory.conn().execute(
        "INSERT OR REPLACE INTO books(id,path,title,format,size_bytes,chars,"
        "pages,status,points_count,dir_id,source_type,created_at,topic) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"b{i:04d}", f"/lib/book{i}.pdf", f"书 {i}", "pdf", 3_000_000,
         320_000, 240, "ready" if i % 3 else "done", i % 7, "d1", "dir",
         time.time() - i * 3600, "方向_01"),
    )
memory.conn().commit()
analytics._reset_caches()

PID = memory.list_points(limit=1)[0]["id"]

CASES = [
    ("/api/status", lambda: api.api_status()),
    ("/api/config GET", lambda: api.api_config_get()),
    ("/api/auto GET", lambda: api.api_auto_get()),
    ("/api/sessions", lambda: api.api_sessions()),
    ("/api/events/recent", lambda: api.api_events_recent(limit=30)),
    ("/api/knowledge (60)", lambda: api.api_knowledge(limit=60)),
    ("/api/knowledge?topic", lambda: api.api_knowledge(topic="方向_01 · 某个具体的学习方向名称", limit=60)),
    ("/api/knowledge?q", lambda: api.api_knowledge(q="知识点 40 字", limit=60)),
    ("/api/knowledge/{pid}", lambda: api.api_knowledge_detail(PID, k=6)),
    ("/api/analytics", lambda: api.api_analytics()),
    ("/api/analytics/health", lambda: api.api_analytics_health()),
    ("/api/analytics/suggestions", lambda: api.api_analytics_suggestions()),
    ("/api/analytics/search", lambda: api.api_analytics_search(q="知识点 40 字")),
    ("/api/perf", lambda: api.api_perf()),
    ("/api/resources", lambda: api.api_resources()),
    ("/api/models", lambda: api.api_models()),
    ("/api/library/stats", lambda: api.api_lib_stats()),
    ("/api/library/dirs", lambda: api.api_lib_dirs()),
    ("/api/library/books", lambda: api.api_lib_books(limit=200)),
    ("/api/library/state", lambda: api.api_lib_state()),
    ("/api/finetune/state", lambda: api.api_ft_state()),
    ("/api/ocr/state", lambda: api.api_ocr_state()),
]

print(f"seed：{N} 条知识点 / 300 本书 / 400 事件；PID={PID}\n")
rows = []
for name, fn in CASES:
    fn()  # 预热（首次建向量缓存 / 缓存冷算）
    iters = 30
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    ms = (time.perf_counter() - t0) / iters * 1000
    rows.append((ms, name))

rows.sort(reverse=True)
print(f"{'端点':<28}{'单次中位':>12}")
print("-" * 44)
for ms, name in rows:
    print(f"{name:<28}{ms:>10.3f} ms")
