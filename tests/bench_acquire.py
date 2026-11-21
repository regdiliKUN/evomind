"""知识获取效率基准：同一批资料，重复学习时会被读几遍？

模拟「持续自学」的真实形状：每次会话新建一个 Engine（`_visited` 清空），
但方向不变，于是同一批 URL 会反复进入检索结果。改动前，抓取成功却没产出
知识的页面不会被记住，所以每一轮都要重新抓取 + 重新提炼。

    .venv\\Scripts\\python.exe tests/bench_acquire.py            # 改动后
    .venv\\Scripts\\python.exe tests/bench_acquire.py --legacy   # 改动前（关掉台账）

统计口径：抓取次数、提炼调用次数、核对调用次数、新增知识点数。
检索/抓取/模型全部用本地假实现，不访问网络、不调用 Ollama。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

CORPUS = 24          # 语料里有多少个 URL
PER_ROUND = 8        # 每轮抓几篇（config.fetch_per_round）
SESSIONS = 5         # 模拟几次「新会话」
ROUNDS = 3           # 每次会话跑几轮
#: 只有这几篇能提炼出知识，其余是营销页/目录页——正是它们造成了重复抓取
FRUITFUL = {2, 9, 17}

TEXT = (
    "DynaMesh 的分辨率参数决定生成网格的拓扑密度，默认值 128 适合中等细节。"
    "提高分辨率会增加多边形数量并显著增加显存占用，实测在 4090 上 512 分辨率"
    "处理一个头部模型需要 18 秒。降低分辨率会损失细小结构，之后需要重新拓扑。"
    "工程上通常先用 128 打底，确认大形之后再升到 256 以上做细节。"
)


def corpus() -> list[dict]:
    return [
        {
            "title": f"资料 {i} · DynaMesh 参数说明",
            "url": f"https://corpus.local/doc/{i}",
            "snippet": "DynaMesh 分辨率与拓扑密度",
            "source": "web",
            "kind": "web",
        }
        for i in range(CORPUS)
    ]


def fake_chat_json(system: str, user: str, **kw):
    """按提示词分流：预筛 / 提炼 / 核对。"""
    if "资料筛选器" in system:
        return {"pick": list(range(1, PER_ROUND + 1)), "why": "按顺序读"}
    if "证据核对员" in system:
        n = len(json.loads(user))
        return {"verified": list(range(n))}
    # 提炼：只有 FRUITFUL 里的文档给得出知识点，其余返回空
    url = ""
    for line in user.splitlines():
        if line.startswith("链接："):
            url = line[3:].strip()
            break
    idx = -1
    if "/doc/" in url:
        try:
            idx = int(url.rsplit("/", 1)[1])
        except ValueError:
            idx = -1
    if idx in FRUITFUL:
        return {"points": [{
            "claim": f"资料 {idx} 指出 DynaMesh 默认分辨率 128 适合中等细节，升到 512 需要约 18 秒。",
            "detail": "分辨率越高多边形越多，显存占用随之上升。",
            "evidence": "DynaMesh 的分辨率参数决定生成网格的拓扑密度",
            "relevance": 0.9, "confidence": 0.8, "tags": ["DynaMesh"],
        }]}
    return {"points": []}


def fake_fetch(url: str):
    # 每篇正文带上自己的编号：真实语料不会 24 篇一字不差，
    # 完全相同的正文会被「跨文档重复段落」过滤掉，那不是本节要测的东西。
    tag = url.rsplit("/", 1)[-1]
    return {"ok": True, "url": url, "title": f"资料 {tag}",
            "text": f"（第 {tag} 篇）" + TEXT * 3, "error": "", "kind": "web"}


def run(legacy: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="evomind-acq-"))
    from backend import config as cfg

    cfg.DATA = tmp
    cfg.DB_FILE = tmp / "knowledge.db"
    from backend import evolve, llm, memory

    counters = {"fetch": 0, "extract": 0, "verify": 0}

    def counted_chat_json(system, user, **kw):
        if "证据核对员" in system:
            counters["verify"] += 1
        elif "资料筛选器" not in system:
            counters["extract"] += 1
        return fake_chat_json(system, user, **kw)

    def counted_fetch(url):
        counters["fetch"] += 1
        return fake_fetch(url)

    rng = np.random.default_rng(11)

    def fake_embed(texts, **kw):
        return [rng.normal(size=64).astype(np.float32).tolist() for _ in texts]

    patches = [
        patch.object(evolve.search, "search", side_effect=lambda q, **kw: corpus()),
        patch.object(evolve.fetcher, "fetch", side_effect=counted_fetch),
        patch.object(evolve.llm, "chat_json", side_effect=counted_chat_json),
        patch.object(evolve.llm, "embed", side_effect=fake_embed),
        patch.object(evolve.engine, "_emit", lambda *a, **k: None),
        patch.object(evolve, "emit", lambda *a, **k: None),
    ]
    if legacy:
        # 改动前：没有台账，只有 learned_documents 拦得住「产出过知识」的页面。
        # 注意 record_attempts 在改动后顺带写 learned_documents，所以这里不能整个
        # 换成空函数——那样连「已学」也一起没了，会低估基线（反而显得改动后更差）。
        patches.append(patch.object(memory, "attempted_urls", lambda *a, **k: set()))

        def legacy_record(urls, topic, **kw):
            for u in kw.get("learned") or ():
                memory.mark_learned(u, topic)

        patches.append(patch.object(memory, "record_attempts", legacy_record))

    from contextlib import ExitStack

    points = 0
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        for s in range(SESSIONS):
            engine = evolve.Engine()          # 新会话：_visited 清空，模拟重启/换会话
            engine.state["direction"] = "ZBrush：DynaMesh 分辨率"
            engine.state["focus"] = "分辨率与拓扑密度"
            for r in range(ROUNDS):
                results = evolve.search.search("ZBrush DynaMesh 分辨率")
                cands = engine._pick_targets(results)
                if not cands:
                    continue
                docs = engine._fetch_all(cands)
                docs = [d for d in docs if d.get("ok")]
                if not docs:
                    continue
                points += len(engine._extract_all(docs, "ZBrush：DynaMesh 分辨率", f"s{s}r{r}"))
    shutil.rmtree(tmp, ignore_errors=True)
    return {"mode": "legacy" if legacy else "current", "sessions": SESSIONS,
            "rounds_per_session": ROUNDS, "corpus": CORPUS,
            "points": points, **counters}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", action="store_true")
    args = ap.parse_args()
    out = run(args.legacy)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
