"""探测：BeautifulSoup(lxml) 与纯 lxml 解析路径的耗时差 + 输出等价性。

用法：python scripts/bench/probe_html.py [rounds]
"""
import random
import re
import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")
import _env  # noqa: E402

_env.isolate()
_env.fake_llm()

from bs4 import BeautifulSoup  # noqa: E402
from lxml import html as lxml_html  # noqa: E402

from backend.fetcher import _clean  # noqa: E402

random.seed(42)

# 造 8 个贴近真实网页的 HTML（80-150KB）：导航、侧栏、脚本、样式、表格、注释
def make_page(i: int) -> str:
    paras = []
    for j in range(160):
        paras.append(
            f"<p>第 {i} 页第 {j} 段：硬表面雕刻的核心在于多边形流与曲率控制，"
            f"当细分级别达到 {j + 3} 时需要权衡面数与渲染预算，这是第 {j} 段足够长的正文内容用于性能对比。</p>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>资料 {i}</title>
<!-- 这是 HTML 注释，解析器行为差异点 -->
<script>var x = {i}; function f() {{ return "<p>not real</p>"; }}</script>
<style>.a{{color:red}} .b{{font-weight:bold}}</style></head>
<body>
<nav><ul><li><a href="/a">导航甲</a></li><li><a href="/b">导航乙</a></li></ul></nav>
<header><h1>资料 {i} 标题</h1><div class="meta">作者 · 2026-10-20</div></header>
<main><article>
{''.join(paras)}
<table><tr><th>参数</th><th>取值</th></tr><tr><td>细分</td><td>4</td></tr></table>
<blockquote>引用块内容也要出现在纯文本里</blockquote>
<br><br>多个 br 之后的内容
</article></main>
<aside><h3>相关</h3><ul><li><a href="/x">链接一</a></li></ul></aside>
<footer>页脚版权信息</footer>
<script>console.log("inline script tail");</script>
</body></html>"""


def bs4_path(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return _clean(soup.get_text("\n", strip=True))


def lxml_path(html: str) -> str:
    doc = lxml_html.fromstring(html)
    skip = {"script", "style", "template", "noscript"}
    parts = []
    stack = [doc]
    while stack:
        el = stack.pop()
        if not isinstance(el.tag, str):  # 注释 / PI：BS4 get_text 不含
            continue
        if el.tag in skip:
            continue
        if el.text:
            t = el.text.strip()
            if t:
                parts.append(t)
        if el.tail:
            t = el.tail.strip()
            if t:
                parts.append(t)
        # 文档序：子节点逆序压栈
        for child in reversed(el):
            stack.append(child)
    return _clean("\n".join(parts))


pages = [make_page(i) for i in range(8)]
print(f"页面数 {len(pages)}，平均 {sum(len(p) for p in pages) // len(pages) // 1024} KB")

# 等价性
same = sum(1 for p in pages if bs4_path(p) == lxml_path(p))
print(f"输出一致页数: {same}/{len(pages)}")
for i, p in enumerate(pages):
    a, b = bs4_path(p), lxml_path(p)
    if a != b:
        # 找第一处差异
        for k in range(min(len(a), len(b))):
            if a[k] != b[k]:
                print(f"  页 {i} 首差@{k}: bs4=...{a[max(0,k-30):k+30]!r} lxml=...{b[max(0,k-30):k+30]!r}")
                break
        else:
            print(f"  页 {i} 长度差: bs4={len(a)} lxml={len(b)}（一方为另一方前缀）")

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 12
ts_bs, ts_lx = [], []
for _ in range(ROUNDS):
    for name, fn, acc in (("bs4", bs4_path, ts_bs), ("lxml", lxml_path, ts_lx)):
        t0 = time.perf_counter()
        for p in pages:
            fn(p)
        acc.append((time.perf_counter() - t0) * 1000)

med = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
print(f"BS4 中位 {med(ts_bs):.1f}ms | 纯 lxml 中位 {med(ts_lx):.1f}ms | 加速 {med(ts_bs) / med(ts_lx):.2f}x")
wins = sum(1 for a, b in zip(ts_bs, ts_lx) if b < a)
print(f"纯 lxml 更快轮次 {wins}/{len(ts_bs)}")
