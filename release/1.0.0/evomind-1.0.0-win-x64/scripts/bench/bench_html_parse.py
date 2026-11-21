"""24 轮交替 A/B：HTML→文本解析，BS4 路径 vs 纯 lxml 路径。

- 负载：10 个贴近真实网页的 HTML（导航/脚本/样式/表格/注释/实体/pre/svg）
- 等价性：逐页字节比对 + 边缘案例集（畸形 HTML、空串、纯文本、实体、未闭合标签）
- 切换：fetcher._USE_BS4（同一份生产代码，只翻开关）

用法：python scripts/bench/bench_html_parse.py [rounds]
"""
import random
import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")
import _env  # noqa: E402

_env.isolate()
_env.fake_llm()

from backend import fetcher  # noqa: E402

random.seed(42)


def make_page(i: int, paras: int = 260) -> str:
    body = "".join(
        f"<p>第 {i} 页第 {j} 段：硬表面雕刻的核心在于多边形流与曲率控制，"
        f"当细分级别达到 {j + 3} 时需要权衡面数与渲染预算，这是第 {j} 段足够长的正文内容。</p>"
        for j in range(paras)
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>资料 {i}</title>
<!-- 解析器行为差异点：HTML 注释 -->
<script>var x = {i}; function f() {{ return "<p>not real</p>"; }}</script>
<style>.a{{color:red}} .b{{font-weight:bold}}</style></head>
<body>
<nav><ul><li><a href="/a">导航甲</a></li><li><a href="/b">导航乙</a></li></ul></nav>
<header><h1>资料 {i} 标题</h1><div class="meta">作者 &amp; 协作者 · 2026-10-20</div></header>
<main><article>
{body}
<table><tr><th>参数</th><th>取值</th></tr><tr><td>细分</td><td>4</td></tr></table>
<blockquote>引用块内容也要出现在纯文本里</blockquote>
<pre>  pre  <b>保留</b>
  换行  </pre>
<br><br>多个 br 之后的内容
<svg><circle cx="1" cy="2" r="3"/></svg>
</article></main>
<aside><h3>相关</h3><ul><li><a href="/x">链接一</a></li></ul></aside>
<footer>页脚版权信息</footer>
<script>console.log("inline script tail");</script>
</body></html>"""


# 边缘案例：两种路径都必须给出相同结果
EDGE_CASES = [
    "",
    "   ",
    "纯文本没有标签",
    "<p>只有一个段落</p>",
    "<div><p>未闭合的 div<p>第二个段落",
    "&lt;p&gt;实体编码&lt;/p&gt; &amp; &quot;引号&quot; &#39;",
    "<script>只有脚本没有正文</script>",
    "<!-- 只有注释 -->",
    "<html><body><table><tr><td>只有表格</td></tr></table></body></html>",
    "<P CLASS='x'>大写标签与属性</P>",
    "<p>嵌套<strong>加粗<em>斜体</em></strong>结束</p>",
    "<div>a</div><div>b</div>",
    "不是 HTML 的 < 孤军 内容",
    "<style>body{color:red}</style><p>样式之后</p>",
]


def run(path_bs4: bool, pages: list[str], edge: list[str]) -> float:
    fetcher._USE_BS4 = path_bs4
    t0 = time.perf_counter()
    for p in pages:
        fetcher._html_to_text(p)
    return (time.perf_counter() - t0) * 1000


def main() -> bool:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    pages = [make_page(i, 120 + (i % 5) * 60) for i in range(10)]

    # ---- 等价性核对（边缘案例 + 正式负载）----
    fetcher._USE_BS4 = True
    bs_edge = [fetcher._html_to_text(h) for h in EDGE_CASES]
    bs_pages = [fetcher._html_to_text(p) for p in pages]
    fetcher._USE_BS4 = False
    lx_edge = [fetcher._html_to_text(h) for h in EDGE_CASES]
    lx_pages = [fetcher._html_to_text(p) for p in pages]
    edge_diff = sum(1 for a, b in zip(bs_edge, lx_edge) if a != b)
    page_diff = sum(1 for a, b in zip(bs_pages, lx_pages) if a != b)
    print(f"等价性：边缘案例 {len(EDGE_CASES) - edge_diff}/{len(EDGE_CASES)} 一致，"
          f"正式页面 {len(pages) - page_diff}/{len(pages)} 一致")
    for i, (a, b) in enumerate(zip(bs_edge, lx_edge)):
        if a != b:
            print(f"  [边缘 {i}] bs4={a[:60]!r} lxml={b[:60]!r}")
    for i, (a, b) in enumerate(zip(bs_pages, lx_pages)):
        if a != b:
            for k in range(min(len(a), len(b))):
                if a[k] != b[k]:
                    print(f"  [页 {i}] 首差@{k}: bs4={a[max(0,k-20):k+20]!r} lxml={b[max(0,k-20):k+20]!r}")
                    break
            else:
                print(f"  [页 {i}] 长度差 bs4={len(a)} lxml={len(b)}")

    # ---- 24 轮交替 A/B ----
    ts_bs, ts_lx = [], []
    for _ in range(rounds):
        ts_bs.append(run(True, pages, EDGE_CASES))
        ts_lx.append(run(False, pages, EDGE_CASES))
    fetcher._USE_BS4 = False

    med = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
    pairs = list(zip(ts_bs, ts_lx))
    wins = sum(1 for a, b in pairs if b < a)
    print(f"BS4 中位 {med(ts_bs):.1f}ms | 纯 lxml 中位 {med(ts_lx):.1f}ms | "
          f"中位加速 {med(ts_bs) / med(ts_lx):.2f}x")
    print(f"纯 lxml 更快轮次 {wins}/{len(pairs)}")
    for i, (a, b) in enumerate(pairs):
        if b >= a:
            print(f"  [轮 {i + 1}] BS4 {a:.1f}ms ≤ lxml {b:.1f}ms")
    ok = wins == len(pairs) and edge_diff == 0 and page_diff == 0
    print("结果：", "通过" if ok else "未过")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
