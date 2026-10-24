"""第十二轮 A/B：学习建议改吃版本缓存聚合（24 轮交替）。

对照组 = 改动前的 suggestions 逐字复刻（每次调用重算 topic_stats /
growth_by_day(30) / source_breakdown + 一次 memory.stats()）。
实验组 = 生产代码 analytics.suggestions()（吃 snapshot 的版本缓存 + 结果按版本缓存）。

负载：2000 条 / 1024 维 / 40 方向，模拟仪表盘 6 秒轮询（稳态无写入）。
等价性：空库 / 单条 / 中等规模 / 薄方向 / 低可信度 / 单信源 六种库形态下，
两实现的四条字段（level/icon/title/detail/view/btn）必须逐字段一致。
"""
from __future__ import annotations

import sys
import time
from datetime import date

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()
_env.fake_llm()

import numpy as np  # noqa: E402

from backend import analytics, config, memory  # noqa: E402

N = 2000
DIM = 1024


# ---------------------------------------------------------------- 旧实现复刻
def old_suggestions(limit: int = 4) -> list[dict]:
    total = memory.count_points()
    items: list[dict] = []

    def add(level, icon, title, detail, view, btn):
        items.append({"level": level, "icon": icon, "title": title,
                      "detail": detail, "view": view, "btn": btn})

    scale_warn = (memory.stats() or {}).get("scale_warn") or ""
    if scale_warn:
        add("warn", "!", "知识库规模提醒", scale_warn, "knowledge", "去整理")

    if total == 0:
        add("act", "◐", "先让它学一个方向",
            "去「自主进化」填一个具体方向，选 3 轮快速摸底。约 10 分钟就有第一批知识，"
            "之后总览和分析页就有东西看了。", "learn", "去学习")
        return items[:limit]

    topics = analytics.topic_stats()
    growth = analytics.growth_by_day(days=30)
    sources = analytics.source_breakdown()

    if growth:
        try:
            last = date.fromisoformat(growth[-1]["day"])
            idle = (date.today() - last).days
        except ValueError:
            idle = 0
        if idle >= 7:
            add("warn", "◔", f"已经 {idle} 天没有新增",
                "学习停滞比学得慢更值得关注。回「自主进化」换个角度，"
                "或就上次的方向再跑几轮，保持知识库的连续性。", "learn", "继续学")

    if len(topics) >= 2:
        top = topics[0]["count"]
        thin = [t for t in topics[1:] if t["count"] <= max(3, top // 8)]
        if thin:
            names = "、".join(t["topic"] for t in thin[:2])
            add("act", "◑", "有方向还比较薄",
                f"{names} 只有 {thin[0]['count']} 条左右。针对性再学几轮，"
                "知识版图会均衡很多。", "learn", "去补盲区")

    low_conf = [t for t in topics if t["avg_conf"] and t["avg_conf"] < 0.5 and t["count"] >= 3]
    if low_conf:
        t = low_conf[0]
        add("warn", "◕", f"「{t['topic']}」可信度偏低",
            f"平均只有 {t['avg_conf']:.2f}。去「检验所学」挑这个方向的问题追问，"
            "答得含糊说明当初的资料不够硬。", "chat", "去检验")

    if sources:
        top_src = sources[0]
        share = top_src["count"] / max(1, total)
        if share >= 0.75 and len(sources) >= 1:
            add("info", "⤢", "资料类型有点单一",
                f"{share * 100:.0f}% 的知识来自{top_src['kind']}。"
                "论文、仓库、问答站的视角不同，混着学更不容易有盲区。",
                "resources", "找找资料")

    if total >= 120:
        add("act", "●", "可以试试权重进化了",
            f"已积累 {total} 条知识。去「权重进化」把它们压进模型参数，"
            "得到一个真正「学会」的版本，再用它回答会明显不一样。", "finetune", "去微调")
    elif total >= 30:
        add("act", "◕", "检验一下学得怎么样",
            f"已有 {total} 条知识。去「检验所学」问几个真问题，"
            "答不上来的地方就是下一步该补的方向。", "chat", "去提问")
    elif total < 30:
        add("info", "◑", "知识还比较薄",
            f"目前 {total} 条，建议再跑几轮加深，或换个角度补盲区。", "learn", "继续学")

    try:
        from backend import finetune

        if not finetune.check_env().get("ready"):
            add("info", "⚙", "微调环境还没装",
                "想用权重层进化，需要先装 PyTorch 等依赖（约 3GB，装一次）。",
                "finetune", "去安装")
    except Exception:
        pass

    add("info", "✎", "随时可以问它",
        "它只用学过的知识回答并标出处，不会拿模型记忆糊弄你。", "chat", "去问")

    order = {"act": 0, "warn": 1, "info": 2}
    items.sort(key=lambda x: order.get(x["level"], 3))
    return items[:limit]


# ---------------------------------------------------------------- 造库
def seed(n: int, topics: int = 40, conf: float = 0.6, kind: str = "web",
         days_spread: int = 7) -> None:
    rng = np.random.default_rng(17)
    now = time.time()
    for i in range(n):
        t = f"方向_{(i % topics):02d} · 某个具体的学习方向名称"
        v = rng.standard_normal(DIM).astype(np.float32)
        v /= np.linalg.norm(v)
        memory.add_point(
            topic=t,
            claim=f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。",
            detail="细节正文。" * 40,
            source_url=f"https://example.com/{i}", source_title=f"资料 {i}",
            source_kind=kind, tags=["a"], confidence=conf,
            vec=v.tolist(), dedup=False,
        )
        # 把 created_at 打散到最近几天：idle 判定要看最后一天
        if days_spread > 0:
            c = memory.conn()
            c.execute(
                "UPDATE knowledge SET created_at=? WHERE claim=?",
                (now - (i % days_spread) * 86400 - 60, f"这是第 {i} 条知识点，claim 长度约 40 字，用于模拟真实语料。"),
            )
            c.commit()
    memory._bump_version()
    analytics._reset_caches()


def wipe() -> None:
    c = memory.conn()
    with c:
        c.execute("DELETE FROM knowledge")
    memory._invalidate()
    memory._bump_search_cache()
    memory._bump_topic_index()
    memory._bump_version()
    analytics._reset_caches()


print("---- 等价性核对（六种库形态） ----")
cases = [
    ("空库", lambda: wipe()),
    ("单条", lambda: (wipe(), seed(1, topics=1))),
    ("中等 300 条", lambda: (wipe(), seed(300, topics=8))),
    ("薄方向（主方向 200 + 尾部长尾）", lambda: (wipe(), seed(200, topics=2), seed(20, topics=6))),
    ("低可信度", lambda: (wipe(), seed(120, topics=6, conf=0.42))),
    ("单信源 + 旧数据", lambda: (wipe(), seed(150, topics=5, kind="book", days_spread=20))),
]
bad = 0
for name, build in cases:
    build()
    old = old_suggestions()
    new = analytics.suggestions()
    same = old == new
    print(f"  {name:28s} {'一致' if same else '不一致'}  ({len(new)} 条)")
    if not same:
        bad += 1
        for a, b in zip(old, new):
            if a != b:
                print("    旧:", a)
                print("    新:", b)
        if len(old) != len(new):
            print(f"    条数不同：{len(old)} vs {len(new)}")
print(f"等价性：{len(cases) - bad}/{len(cases)} 通过")

# ---------------------------------------------------------------- 24 轮 A/B
print("\n---- 24 轮交替 A/B（稳态轮询，2000 条 / 1024 维 / 40 方向） ----")
wipe()
seed(N)
analytics.snapshot()  # 预热：让 snapshot 缓存就位（真实场景里分析页/健康卡片已拉过一次）

# 稳态：库里没有新写入，只有轮询
old_ts, new_ts = [], []
for r in range(24):
    if r % 2 == 0:
        t = time.perf_counter(); old_suggestions(); old_ts.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); analytics.suggestions(); new_ts.append((time.perf_counter() - t) * 1000)
    else:
        t = time.perf_counter(); analytics.suggestions(); new_ts.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); old_suggestions(); old_ts.append((time.perf_counter() - t) * 1000)

old_ts.sort(); new_ts.sort()
om = old_ts[len(old_ts) // 2]; nm = new_ts[len(new_ts) // 2]
wins = sum(1 for a, b in zip(old_ts, new_ts) if b < a)
print(f"  旧实现（每次重算）中位 {om:7.3f} ms   最小 {old_ts[0]:7.3f} ms")
print(f"  新实现（版本缓存）中位 {nm:7.3f} ms   最小 {new_ts[0]:7.3f} ms")
print(f"  新实现更快轮次：{wins}/24   加速比 {om / nm if nm else float('inf'):.1f}x")

# 轮询风暴：60 秒 / 6 秒一次 = 10 次
def storm(fn, times: int = 10) -> float:
    t = time.perf_counter()
    for _ in range(times):
        fn()
    return (time.perf_counter() - t) * 1000


analytics._reset_caches()
analytics.snapshot()
a = storm(old_suggestions)
analytics._reset_caches()
analytics.snapshot()
b = storm(analytics.suggestions)
print(f"  10 次轮询（60 秒）：旧 {a:.2f} ms → 新 {b:.2f} ms，每小时每标签页 "
      f"{a * 6 / 1000:.2f} s → {b * 6 / 1000:.2f} s")

# 写入后仍必须反映新数据（版本接线）
print("\n---- 写入后一致性 ----")
seed(30, topics=3)
old = old_suggestions(); new = analytics.suggestions()
print(f"  写入 30 条后：{'一致' if old == new else '不一致'}")
t = time.perf_counter(); analytics.suggestions(); print(f"  写入后首次建议耗时 {(time.perf_counter() - t) * 1000:.3f} ms（重算一次，之后复用）")

# 相似知识点（新功能）正确性
print("\n---- 相似知识点 ----")
pid = memory.list_points(limit=1)[0]["id"]
sims = memory.similar_points(pid, k=5)
print(f"  取到 {len(sims)} 条邻居，相似度：{[s['similarity'] for s in sims]}")
print(f"  自身未被包含：{all(s['id'] != pid for s in sims)}")
print(f"  降序：{all(sims[i]['similarity'] >= sims[i + 1]['similarity'] for i in range(len(sims) - 1))}")
t = time.perf_counter()
for _ in range(20):
    memory.similar_points(pid, k=5)
print(f"  similar_points 单次 {(time.perf_counter() - t) / 20 * 1000:.3f} ms")
