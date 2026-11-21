"""探针（第十三轮 B）：① 定位 /api/models 与 /api/status 的异常耗时来源；

② 检查 1 万条规模下是否有随规模平方级退化的路径（明细：缓存构建 / 检索 /
   主题网络 / 详情近邻 / 列表分页），避免只在 2000 条这个舒服区间做优化。
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

from backend import analytics, finetune, llm, memory  # noqa: E402


def t(label: str, fn, iters: int = 5) -> float:
    fn()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    ms = (time.perf_counter() - t0) / iters * 1000
    print(f"  {label:<34}{ms:>10.2f} ms")
    return ms


print("== ① /api/models 与 /api/status 的耗时来源 ==")
llm._invalidate_tags()
finetune._env_cache.update(ts=0.0, value=None)
t("llm.list_models() 冷（远端往返）", llm.list_models, 3)
t("llm.list_models() 热（仍每次直连）", llm.list_models, 3)
t("llm.tags() 冷", lambda: (llm._invalidate_tags(), llm.tags()), 3)
t("llm.tags() 热（命中缓存）", llm.tags, 5)
t("finetune.check_env() 冷（import torch）", lambda: (finetune._env_cache.update(ts=0.0, value=None), finetune.check_env()), 3)
print("  torch 已 import 后的纯探测：")
t("  torch.cuda.is_available()", lambda: __import__("torch").cuda.is_available(), 5)

print("\n== ② 1 万条规模下的关键路径 ==")
N = 10000
DIM = 1024
rng = np.random.default_rng(23)
vecs = rng.standard_normal((N, DIM)).astype(np.float32)
vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
recs = [
    (f"方向_{(i % 60):02d} · 某个具体的学习方向名称",
     f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。",
     f"https://example.com/{i}", vecs[i])
    for i in range(N)
]
t0 = time.perf_counter()
for topic, claim, url, v in recs:
    memory.add_point(topic=topic, claim=claim, detail="细节正文。" * 60,
                     source_url=url, source_title="资料", source_kind="web",
                     tags=["a", "b"], confidence=0.6, vec=v.tolist(), dedup=False)
print(f"  seed 10000 条用时 {time.perf_counter() - t0:.1f}s")
memory._bump_search_cache()
analytics._reset_caches()

t("memory.stats()", memory.stats, 5)
t("memory.count_points()", memory.count_points, 5)
t("memory.list_points(60)", lambda: memory.list_points(limit=60), 5)
t("memory.search() 冷", lambda: (memory._bump_search_cache(), memory.search("知识点 40 字"))[1], 3)
t("memory.search() 热（结果缓存）", lambda: memory.search("知识点 40 字"), 5)
t("_all_vectors 冷建（1 万×1024）", lambda: (memory._invalidate(), memory._all_vectors(DIM))[1], 2)
t("analytics.snapshot(fresh) 冷算", lambda: analytics.snapshot(fresh=True), 3)
t("analytics.topic_graph() 冷", lambda: (analytics._GRAPH_CACHE.update(ver=-1, val=None),
                                         analytics.topic_graph())[1], 3)
pid = memory.list_points(limit=1)[0]["id"]
t("memory.similar_points(k=6)", lambda: memory.similar_points(pid, 6), 3)
