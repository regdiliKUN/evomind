"""第五轮 A/B：检索结果缓存（memory.search 按 (query,topic,k) 命中）。

交替对比：
  - 冷路径（baseline）：每次检索前清空结果缓存，等于「原实现每次都重算」
  - 热路径（cached）：  重复查询命中缓存，等于新实现

库固定（4000 条），每轮用同一组查询各重复多次，模拟仪表盘轮询 / 分析视图反复打开。
等价性：静态库下冷路径与热路径对每个查询返回的命中集合必须完全一致。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402
_env.isolate()
_env.fake_llm()

from backend import memory  # noqa: E402

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
N_SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
N_QUERIES = 12
REPEAT = 4  # 每个查询重复次数（模拟轮询）


def seed(n: int) -> None:
    c = memory.conn()
    c.execute("DELETE FROM knowledge")
    c.commit()
    memory._invalidate()
    memory._bump_search_cache()
    for i in range(n):
        memory.add_point(
            topic=f"方向{i % 7}",
            claim=f"关于方向{i%7}的知识点内容编号{i} 用于检索缓存基准测试的语义检索与重复查询命中率验证。",
            detail=f"细节内容 {i} " * 15,
            source_kind="book",
            confidence=0.5 + (i % 5) * 0.1,
            dedup=False,
        )
    memory._invalidate()
    memory._bump_search_cache()


def make_queries(n: int) -> list[str]:
    # 取自已有 claim 的变体，保证能检索到内容（覆盖率也更高）
    return [f"关于方向{i % 7}的知识点内容编号{i} 用于检索缓存基准测试的语义检索与重复查询命中率验证。" for i in range(n)]


def run_cold(queries: list[str]) -> float:
    t0 = time.perf_counter()
    for q in queries:
        for _ in range(REPEAT):
            memory._search_cache.clear()  # 强制冷算
            memory.search(q, k=8)
    return time.perf_counter() - t0


def run_cached(queries: list[str]) -> float:
    memory._search_cache.clear()
    t0 = time.perf_counter()
    for q in queries:
        for _ in range(REPEAT):
            memory.search(q, k=8)  # 第 1 次算，其后命中
    return time.perf_counter() - t0


def equivalence_ok(queries: list[str]) -> bool:
    """静态库下，冷算结果与缓存结果必须逐查询一致。"""
    memory._search_cache.clear()
    cold = {q: memory.search(q, k=8) for q in queries}
    memory._search_cache.clear()
    hot = {q: memory.search(q, k=8) for q in queries}
    for q in queries:
        a = [(h["claim"], h["score"]) for h in cold[q]]
        b = [(h["claim"], h["score"]) for h in hot[q]]
        if a != b:
            return False
    return True


def main() -> None:
    seed(N_SEED)
    queries = make_queries(N_QUERIES)
    print(f"库规模={N_SEED}  查询数={N_QUERIES}  每查询重复={REPEAT}  轮数={ROUNDS}")
    print(f"等价性核对（冷==热）: {'通过' if equivalence_ok(queries) else '失败!!!'}\n")

    cold_times: list[float] = []
    cached_times: list[float] = []
    for r in range(1, ROUNDS + 1):
        # 交替顺序，消除先后偏差
        if r % 2 == 1:
            tc = run_cold(queries)
            th = run_cached(queries)
        else:
            th = run_cached(queries)
            tc = run_cold(queries)
        cold_times.append(tc)
        cached_times.append(th)
        speedup = tc / th if th > 0 else float("inf")
        print(f"  轮 {r:2d}: 冷 {tc*1000:7.1f}ms | 热 {th*1000:7.1f}ms | 加速 {speedup:5.2f}x")

    wins = sum(1 for c, h in zip(cold_times, cached_times) if h < c)
    ratios = sorted(c / h for c, h in zip(cold_times, cached_times) if h > 0)
    med = ratios[len(ratios) // 2]
    mean = sum(c / h for c, h in zip(cold_times, cached_times) if h > 0) / len(ratios)
    tot_c = sum(cold_times)
    tot_h = sum(cached_times)
    print(f"\n胜场: {wins}/{ROUNDS} 轮热路径更快")
    print(f"加速比: 中位 {med:.2f}x | 均值 {mean:.2f}x | 合计 {tot_c/tot_h:.2f}x")
    print(f"合计耗时: 冷 {tot_c*1000:.0f}ms → 热 {tot_h*1000:.0f}ms")


if __name__ == "__main__":
    main()
