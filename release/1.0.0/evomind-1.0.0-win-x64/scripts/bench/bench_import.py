"""书库导入基准：扫描 / 解析 / 切章 / 切块 / 入库 各阶段耗时与调用次数。

把 LLM 换成确定性假实现（--fake-llm，默认开），这样测的是我们自己的代码路径，
而不是 Ollama 的排队时间；向量维度仍是 1024，向量矩阵重建的开销是真实的。

    .venv/Scripts/python.exe scripts/bench/bench_import.py
    .venv/Scripts/python.exe scripts/bench/bench_import.py --preload 3000
    .venv/Scripts/python.exe scripts/bench/bench_import.py --real-book "K:/x/y.pdf"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

import _env  # noqa: F401

FIX = _env.FIXTURES


class Probe:
    """给函数套一层计时 / 计数。"""

    def __init__(self) -> None:
        self.stats: dict[str, dict] = {}

    def wrap(self, mod, name: str, key: str) -> None:
        fn = getattr(mod, name)
        st = self.stats.setdefault(key, {"n": 0, "t": 0.0})
        import functools

        @functools.wraps(fn)
        def inner(*a, **kw):
            t0 = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                st["n"] += 1
                st["t"] += time.perf_counter() - t0

        setattr(mod, name, inner)

    def report(self, title: str) -> None:
        print(f"\n--- {title} ---")
        if not self.stats:
            print("  (无采样)")
            return
        for k, v in self.stats.items():
            avg = v["t"] / v["n"] * 1000 if v["n"] else 0
            print(f"  {k:<22} 调用 {v['n']:>5} 次 · 合计 {v['t']*1000:9.1f} ms · 平均 {avg:8.2f} ms")  # noqa: E501


def timed(label: str, fn, repeat: int = 1):
    ts = []
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        ts.append(time.perf_counter() - t0)
    best = min(ts)
    print(f"  {label:<34} {statistics.mean(ts)*1000:9.1f} ms  (最快 {best*1000:.1f} ms, n={repeat})")  # noqa: E501
    return out, statistics.mean(ts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preload", type=int, default=0,
                    help="先塞入 N 条知识点，模拟已有语料（放大入库阶段开销）")
    ap.add_argument("--real-book", default="", help="额外测一本真实 PDF")
    ap.add_argument("--no-fake-llm", action="store_true")
    ap.add_argument("--real-embed", action="store_true",
                    help="保留真实的向量化调用（bge-m3），用来测批量 embed 的收益")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    _env.isolate()
    if not args.no_fake_llm:
        _env.fake_llm(keep_real_embed=args.real_embed)

    from backend import config, fetcher, library, memory

    probe = Probe()
    probe.wrap(library, "_extract_text", "parse/提取正文")
    probe.wrap(library, "_split_chapters", "split/切章")
    probe.wrap(library, "parse_book", "parse_book(整本)")
    probe.wrap(library, "_read_text_file", "read/读文本文件")
    probe.wrap(fetcher, "chunk", "chunk/切块")
    probe.wrap(memory, "_all_vectors", "vec/重建向量矩阵")
    probe.wrap(memory, "add_point", "write/写入知识点")

    if args.preload:
        print(f"预置 {args.preload} 条知识点…")
        t0 = time.perf_counter()
        for i in range(args.preload):
            memory.add_point(
                topic="预置语料", claim=f"预置知识点 {i}：这是一条用于压测向量检索的句子。",
                detail="填充内容。" * 20, source_kind="bench", dedup=False,
            )
        print(f"  预置完成 {(time.perf_counter()-t0):.1f}s · 现有 "
              f"{memory.conn().execute('SELECT COUNT(*) n FROM knowledge').fetchone()['n']} 条")

    results: dict[str, float] = {}

    # ---------------- 1. 扫描 ----------------
    print("\n===== 1. 扫描目录（登记进 books 表） =====")
    tree = FIX / "scan_tree"
    if not tree.exists():
        print("  缺少语料，先跑 scripts/bench/make_fixtures.py")
        return 1
    library.init_db()
    c = memory.conn()

    def fresh_scan_tree() -> None:
        c.execute("DELETE FROM books")
        c.execute("DELETE FROM library_dirs")
        c.commit()
        library.add_dir(str(tree), label="bench")

    # 空表首次扫描
    fresh_scan_tree()
    _, results["scan_fresh"] = timed("scan 5000 文件（空表首次扫描）",
                                     lambda: library.scan(), repeat=1)
    n1 = c.execute("SELECT COUNT(*) n FROM books").fetchone()["n"]
    # 表内已有全部记录后重扫（真实用法：书库反复扫描）
    for _ in range(2):
        _, el = timed("scan 5000 文件（books 已有全部记录）",
                      lambda: library.scan(), repeat=1)
    results["scan"] = el
    print(f"  登记 {n1} 条书目，重扫后仍为 "
          f"{c.execute('SELECT COUNT(*) n FROM books').fetchone()['n']} 条（未重复登记）")

    # ---------------- 2. 解析 ----------------
    print("\n===== 2. 解析（读文件 → 提取正文） =====")
    c.execute("DELETE FROM books")
    c.execute("DELETE FROM library_dirs")
    c.commit()
    targets = [
        ("text_book.pdf", FIX / "text_book.pdf"),
        ("book.epub", FIX / "book.epub"),
        ("book.docx", FIX / "book.docx"),
        ("plain.txt", FIX / "plain.txt"),
        ("gbk_book.txt", FIX / "gbk_book.txt"),
        ("big.txt", FIX / "big.txt"),
    ]
    if args.real_book and Path(args.real_book).exists():
        targets.append((Path(args.real_book).name, Path(args.real_book)))

    for name, p in targets:
        if not p.exists():
            print(f"  跳过 {name}（不存在）")
            continue
        bid = library_scan_one(str(p))
        out, el = timed(f"parse {name} ({p.stat().st_size/1024:.0f} KB)",
                        lambda: library.parse_book(bid), repeat=args.repeat)
        results[f"parse:{name}"] = el
        if out and out.get("ok"):
            print(f"      → {out.get('chars')} 字 / {out.get('chapters')} 章")

    # ---------------- 3. 切章 + 切块（纯算法） ----------------
    print("\n===== 3. 切章 / 切块（纯 CPU） =====")
    big = (FIX / "big.txt").read_text(encoding="utf-8")
    print(f"  语料 big.txt：{len(big)} 字")
    _, results["split_big"] = timed("_split_chapters(big.txt)",
                                    lambda: library._split_chapters(big, "t"),
                                    repeat=args.repeat)
    chapters = library._split_chapters(big, "t")
    print(f"      → {len(chapters)} 章")
    _, results["chunk_big"] = timed("chunk(全部章节)", lambda: [
        x for ch in chapters for x in fetcher.chunk(ch["text"], size=2200, overlap=200)
    ], repeat=args.repeat)

    # ---------------- 4. 入库 ----------------
    print("\n===== 4. 提炼入库 import_book（LLM 为假实现） =====")
    probe2 = Probe()
    probe2.wrap(library, "_extract_text", "parse/提取正文")
    probe2.wrap(library, "_split_chapters", "split/切章")
    probe2.wrap(fetcher, "chunk", "chunk/切块")
    probe2.wrap(memory, "_all_vectors", "vec/重建向量矩阵")
    probe2.wrap(memory, "add_point", "write/写入知识点")

    bid = library_scan_one(str(FIX / "text_book.pdf"))
    t0 = time.perf_counter()
    r = library.import_book(bid, topic="基准测试", max_chunks=30)
    el = time.perf_counter() - t0
    results["import_text_book"] = el
    print(f"  import_book(text_book, max_chunks=30)  {el*1000:.1f} ms → {r}")
    probe2.report("import_book 内部阶段")
    results["import_vec"] = probe2.stats.get("vec/重建向量矩阵", {}).get("t", 0)
    results["import_write"] = probe2.stats.get("write/写入知识点", {}).get("t", 0)
    results["import_split"] = probe2.stats.get("split/切章", {}).get("t", 0)
    results["import_parse"] = probe2.stats.get("parse/提取正文", {}).get("t", 0)
    results["import_points"] = r.get("points", 0)

    # 重复导入同一本（走 status=done 分支，会再解析一遍）
    probe3 = Probe()
    probe3.wrap(library, "_extract_text", "parse/提取正文")
    probe3.wrap(library, "_split_chapters", "split/切章")
    probe3.wrap(memory, "_all_vectors", "vec/重建向量矩阵")
    bid2 = library_scan_one(str(FIX / "text_book.pdf"))
    library.parse_book(bid2)
    t0 = time.perf_counter()
    r2 = library.import_book(bid2, topic="基准测试", max_chunks=30)
    el2 = time.perf_counter() - t0
    results["import_again"] = el2
    print(f"  再次 import（status 已 ready/done）    {el2*1000:.1f} ms → {r2}")
    probe3.report("重复导入内部阶段")

    print("\n===== 机器可读结果 =====")
    print("RESULT_JSON " + json.dumps({k: round(v, 4) for k, v in results.items()}))
    return 0


def library_scan_one(path: str) -> str:
    """把单个文件登记进 books 表，返回 book_id。"""
    from backend import library, memory

    library.init_db()
    c = memory.conn()
    p = Path(path)
    bid = hashlib.md5(str(p).encode("utf-8")).hexdigest()[:16]
    c.execute(
        "INSERT OR REPLACE INTO books(id,dir_id,path,source_type,format,title,"
        "size_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (bid, "", str(p), "local", p.suffix.lstrip("."), p.stem,
         p.stat().st_size, "pending", time.time()),
    )
    c.commit()
    return bid


if __name__ == "__main__":
    sys.exit(main())
