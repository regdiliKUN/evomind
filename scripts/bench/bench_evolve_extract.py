"""Round 7 A/B：进化引擎入库 —— 逐条 add_point（现状）vs add_points_batch（优化）。

`evolve._extract_all` 目前对每条提炼出的知识点单独调一次 `memory.add_point`：
每写一条就做一次「全库向量相似度」＋一次事务提交（commit 走 WAL fsync，实测约 2ms/次）。
一轮进化从若干篇资料里抽出几十条，就是几十次 commit，且随库内条数线性变慢。
第四轮已把「书库导入」改成批量，但进化引擎这条写入路径一直没改。

基准分两部分：
  Part 1（计时，头条）：库一次性建好、之后随轮增长且**不 invalidate**（贴合生产：
     进化跨多轮连续写，向量缓存靠 _append_vector 保持热）。逐轮交替 逐条/批量，
     各加 48 条全新候选，配对比较；并逐轮断言两条路径接受条数一致。
  Part 2（等价性）：在一条与 Part1 无关的小库上，逐条 vs 批量各跑一次同一批候选，
     逐条比对接受的 claim 集合与顺序，必须完全一致。

用法：bench_evolve_extract.py [轮数] [基线库规模] [每轮候选数]
"""
import sys, time, random
sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")
import _env; _env.isolate(); _env.fake_llm()
from backend import memory

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
BASE = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
PER = int(sys.argv[3]) if len(sys.argv) > 3 else 48
TOPIC = "进化压测方向"
SID = "bench-sid"


def seed():
    for _ in range(BASE):
        memory.add_point(
            topic=f"方向{random.randint(0, 11)}",
            claim=f"基线知识点 {random.random()} 用于给进化入库压测提供一定规模的既有知识库内容。",
            detail="基线细节 " * 12, source_kind="web", confidence=0.6, dedup=False,
        )


def gen_cands(r: int) -> list[dict]:
    cands = []
    for i in range(PER):
        cands.append(
            {
                "topic": TOPIC,
                "claim": f"进化第{r}轮候选知识点 {i} {random.random()} 关于机制原理与量化参数边界条件的具体结论，句子足够长以通过长度校验。",
                "detail": "补充细节内容 " * 8,
                "tags": ["auto"],
                "confidence": round(random.uniform(0.4, 0.9), 2),
                "source_url": f"https://example.com/{r}/{i}",
                "source_title": f"资料 {i}",
                "source_kind": random.choice(["web", "web", "repo", "paper"]),
            }
        )
    # 注入一条批内重复（模拟模型从不同来源抽出同一条）→ 期望接受 PER 条（48 里 1 条被批内去重）
    cands.append(dict(cands[0]))
    return cands


def run_single(cands: list[dict]) -> tuple[float, int]:
    n = 0
    t0 = time.perf_counter()
    for p in cands:
        pid = memory.add_point(
            topic=p["topic"], claim=p["claim"], detail=p["detail"],
            source_url=p["source_url"], source_title=p["source_title"],
            source_kind=p["source_kind"], tags=p["tags"],
            confidence=p["confidence"], session_id=SID,
        )
        if pid:
            n += 1
    return (time.perf_counter() - t0) * 1000, n


def run_batch(cands: list[dict]) -> tuple[float, int]:
    t0 = time.perf_counter()
    res = memory.add_points_batch(cands, session_id=SID)
    return (time.perf_counter() - t0) * 1000, res["added"]


def part1_timing():
    seed()
    print(f"[Part1 计时] 基线库 {memory.count_points()} 条，之后随轮增长、不 invalidate | 每轮候选 {PER}+1 | 轮数 {ROUNDS}")
    ts_s, ts_b = [], []
    count_mism = 0
    random.seed(2026)
    for r in range(ROUNDS):
        cands = gen_cands(r)
        if r % 2 == 0:
            t, n = run_single(cands)
            ts_s.append(t)
        else:
            t, n = run_batch(cands)
            ts_b.append(t)
        if n != PER:  # 每轮应恰好接受 PER 条（48 全新 + 1 批内重复被去重）
            count_mism += 1
    # 配对：第 k 个 single 轮 vs 第 k 个 batch 轮（相邻，库规模相差 96，偏向保守）
    pairs = list(zip(ts_s, ts_b))
    wins = sum(1 for s, b in pairs if b < s)
    med_s = sorted(ts_s)[len(ts_s) // 2]
    med_b = sorted(ts_b)[len(ts_b) // 2]
    losses = [(i, s, b) for i, (s, b) in enumerate(pairs) if b >= s]
    print(f"逐条中位 {med_s:.1f}ms | 批量中位 {med_b:.1f}ms | 中位加速 {med_s/med_b:.2f}x")
    print(f"批量更快轮次 {wins}/{len(pairs)} | 接受条数不符期望: {count_mism}")
    for i, s, b in losses:
        print(f"  · 第 {i} 对批量未更快：逐条 {s:.1f}ms vs 批量 {b:.1f}ms（差 {b - s:+.1f}ms）")
    # 判定：批量在 ≥80% 配对中更快、中位加速 ≥1.3x、且接受条数符合期望
    need = -(-len(pairs) * 8 // 10)  # ceil(80%)
    ok = (wins >= need) and (med_s / med_b >= 1.3) and (count_mism == 0)
    print(f"[Part1 判定] {wins}/{len(pairs)} 更快（需 ≥{need}），中位 {med_s/med_b:.2f}x（需 ≥1.3x）→ {'OK' if ok else 'FAIL'}")
    return ok


def part2_equivalence():
    # 独立小库， wiped 后分别跑两条路径同一批候选
    memory.conn().execute("DELETE FROM knowledge")
    memory.conn().commit()
    memory._invalidate(); memory._bump_search_cache(); memory._bump_topic_index()
    for i in range(300):
        memory.add_point(topic=f"底{i%4}", claim=f"等价性基线 {i} {random.random()} 足够长的句子用于提供去重靶子。", detail="d" * 30, dedup=False)
    cands = gen_cands(999)
    # 逐条
    single_claims = []
    for p in cands:
        if memory.add_point(topic=p["topic"], claim=p["claim"], detail=p["detail"],
                            source_url=p["source_url"], source_title=p["source_title"],
                            source_kind=p["source_kind"], tags=p["tags"],
                            confidence=p["confidence"], session_id=SID):
            single_claims.append(p["claim"])
    # 清掉，恢复
    c = memory.conn()
    with c:
        c.executemany("DELETE FROM knowledge WHERE claim=?", [(x,) for x in single_claims])
    memory._invalidate(); memory._bump_search_cache(); memory._bump_topic_index()
    # 批量
    res = memory.add_points_batch([dict(p) for p in cands], session_id=SID)
    batch_claims = list(res["accepted"])
    set_ok = set(single_claims) == set(batch_claims)
    order_ok = single_claims == batch_claims
    print(f"[Part2 等价性] 逐条接受 {len(single_claims)} | 批量接受 {len(batch_claims)} | 集合一致 {set_ok} | 顺序一致 {order_ok}")
    return set_ok and order_ok


def main():
    ok1 = part1_timing()
    ok2 = part2_equivalence()
    print("结论:", "pass OK" if (ok1 and ok2) else "FAIL")


if __name__ == "__main__":
    main()
