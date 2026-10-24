"""Round 6 A/B：search(topic=...) 主题过滤去 DB 化 + 掩码相似度。

对照方式：同一份查询集，在同一库上分别跑
  - 朴素路径（memory._DISABLE_TOPIC_INDEX=True）：每次打 `SELECT rowid WHERE topic=?` + 整块矩阵复制
  - 优化路径（默认）：内存主题索引 + 布尔掩码，零 DB 往返、零矩阵复制
两路结果必须逐条一致；24 轮配对，统计「优化更快」的轮次数 + 中位加速比。

用法：bench_topic_search.py [轮数] [库规模] [每轮检索数]
"""
import sys, time, random
sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")
import _env; _env.isolate(); _env.fake_llm()
from backend import memory, llm

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
LIB = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
K = int(sys.argv[3]) if len(sys.argv) > 3 else 120
DENSE = 8
SPARSE_TOPIC = "冷门方向X"


def seed():
    for _ in range(LIB):
        t = f"方向{random.randint(0, DENSE - 1)}"
        memory.add_point(
            topic=t,
            claim=f"知识点内容 {random.random()} 用于第六轮主题过滤检索去 DB 化与掩码相似度压测，确保结果零偏差且稳定更快。",
            detail="细节内容 "*15, source_kind="web", confidence=0.6, dedup=False,
        )
    # 稀疏主题：点数远小于 k，专门验证掩码不会把非本主题行泄漏进结果
    for i in range(3):
        memory.add_point(
            topic=SPARSE_TOPIC,
            claim=f"冷门方向专有知识点 {i} 仅用于验证小主题检索不会返回无关行。",
            detail="冷门细节 "*15, source_kind="web", confidence=0.7, dedup=False,
        )


def run_path(disable: bool, queries_topics: list) -> tuple[float, list[set]]:
    memory._DISABLE_TOPIC_INDEX = disable
    memory._search_cache.clear()
    t0 = time.perf_counter()
    sets = []
    for q, t in queries_topics:
        v = llm.embed([q])[0]
        hits = memory.search(q, k=8, topic=t, vec=v)
        sets.append({h["id"] for h in hits})
    dt = (time.perf_counter() - t0) * 1000
    return dt, sets


def main():
    seed()
    topics = [t for t, in memory.conn().execute("SELECT DISTINCT topic FROM knowledge")]
    n_sparse = memory.count_points(SPARSE_TOPIC)
    print(f"库规模 {memory.count_points()} | 主题 {len(topics)}（含稀疏主题 {SPARSE_TOPIC} 仅 {n_sparse} 条）")
    print(f"轮数 {ROUNDS} | 每轮 {K} 次主题过滤检索")

    wins = 0
    ts_n, ts_o = [], []
    mism = 0
    sparse_leak = 0
    random.seed(1234)
    for r in range(ROUNDS):
        qs = [(f"第六轮压测题面 {random.random()} 主题过滤检索验证", random.choice(topics))
              for _ in range(K)]
        # 每轮混入若干稀疏主题查询
        qs += [(f"冷门方向校验 {random.random()} 小主题检索泄漏测试", SPARSE_TOPIC)
               for _ in range(8)]

        tn, naive_sets = run_path(True, qs)
        to, opt_sets = run_path(False, qs)
        ts_n.append(tn); ts_o.append(to)
        if to < tn:
            wins += 1
        # 等价性：逐查询比对命中 id 集合
        for a, b in zip(naive_sets, opt_sets):
            if a != b:
                mism += 1
        # 稀疏主题泄漏检查：优化路径返回数不得超过该主题实际条数
        for (q, t), s in zip(qs, opt_sets):
            if t == SPARSE_TOPIC and len(s) > n_sparse:
                sparse_leak += 1

    med_n = sorted(ts_n)[ROUNDS // 2]
    med_o = sorted(ts_o)[ROUNDS // 2]
    print(f"\n朴素中位 {med_n:.1f}ms | 优化中位 {med_o:.1f}ms | 中位加速 {med_n/med_o:.2f}x")
    print(f"优化更快轮次 {wins}/{ROUNDS}")
    print(f"命中集合不一致（误差）: {mism}")
    print(f"稀疏主题泄漏（误返回非本主题行）: {sparse_leak}")
    ok = (wins == ROUNDS) and (mism == 0) and (sparse_leak == 0)
    print("结论:", "通过 ✅" if ok else "未通过 ❌")
    memory._DISABLE_TOPIC_INDEX = False


if __name__ == "__main__":
    main()
