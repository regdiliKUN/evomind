"""生成确定性测试语料，供导入 / OCR 基准使用。

同样的种子 → 同样的文件（字节级一致），所以优化前后的对比是可复现的。

    .venv/Scripts/python.exe scripts/bench/make_fixtures.py
"""
from __future__ import annotations

import random
import shutil
from pathlib import Path

import _env  # noqa: F401  (设置 sys.path / BENCH_DIR)

FIX = _env.FIXTURES

SUBJECT = "数控加工与刀具选型"
_WORDS = (
    "切削速度进给量背吃刀量主轴转速刀具前角后角主偏角副偏角刃倾角"
    "硬质合金涂层刀片陶瓷刀具立方氮化硼金刚石刀具耐磨性红硬性抗弯强度"
    "断屑槽切屑形态积屑瘤表面粗糙度尺寸精度形位公差定位基准夹紧力"
    "冷却液乳化液油雾润滑干切削振动颤振让刀误差补偿热变形残余应力"
    "铣削车削钻削镗削磨削铰孔攻丝拉削滚齿插齿电火花线切割"
    "不锈钢钛合金高温合金淬硬钢铸铁铝合金镁合金复合材料碳纤维"
    "工艺规程工序卡切削用量工时定额刀具寿命磨损曲线月牙洼崩刃"
)


def _para(rnd: random.Random, n: int) -> str:
    return "".join(rnd.choice(_WORDS) for _ in range(n))


def _chapter_text(rnd: random.Random, ci: int, n_paras: int) -> str:
    """生成一章：标题行 + 若干段落。"""
    out = [f"第{ci}章  {SUBJECT} · 第 {ci} 节\n"]
    for p in range(n_paras):
        out.append(
            f"{p+1}. {_para(rnd, 60)}，{_para(rnd, 40)}。"
            f"{_para(rnd, 55)}；{_para(rnd, 45)}。"
        )
        if p % 4 == 3:
            out.append("")  # 段落间距，让 chunk 按段落边界切
    return "\n".join(out)


#: 每页行数 / 每行字数，与真实排版同量级
LINES_PER_PAGE = 34
CHARS_PER_LINE = 44


def build_text_pdf(path: Path, chapters: int, rnd: random.Random,
                   paras_per_chapter: int = 60) -> int:
    """文字版 PDF：有文字层，library.parse_pdf 走全量提取。"""
    import pymupdf

    doc = pymupdf.open()
    pages = 0
    for ci in range(1, chapters + 1):
        lines: list[str] = []
        for ln in _chapter_text(rnd, ci, paras_per_chapter).split("\n"):
            # 长段落按每行字数折行，保证每页塞满、页数可预期
            if len(ln) <= CHARS_PER_LINE:
                lines.append(ln)
            else:
                lines.extend(
                    ln[i : i + CHARS_PER_LINE]
                    for i in range(0, len(ln), CHARS_PER_LINE)
                )
        for i in range(0, len(lines), LINES_PER_PAGE):
            page = doc.new_page()
            y = 56
            for ln in lines[i : i + LINES_PER_PAGE]:
                if ln:
                    try:
                        page.insert_text(
                            (52, y), ln[:CHARS_PER_LINE], fontsize=10.5,
                            fontname="china-s",
                        )
                    except Exception:  # noqa: BLE001
                        page.insert_text((52, y), "x" * 20, fontsize=10.5)
                y += 21
                if y > 790:
                    break
            pages += 1
    doc.save(str(path), deflate=True, garbage=4)
    doc.close()
    return pages


def build_scanned_pdf(path: Path, src: Path, dpi: int = 200,
                      jpg_quality: int = 80) -> int:
    """扫描版 PDF：把文字版每页渲染成 JPEG 再嵌入，只保留图像层（无文字层）。

    用 JPEG 而不是原始位图，是为了让体积贴近真实扫描书（每页几百 KB），
    否则 20 页就是 200MB，渲染基准测的其实是磁盘 I/O 而不是解码。
    """
    import pymupdf

    src_doc = pymupdf.open(str(src))
    out = pymupdf.open()
    n = 0
    for page in src_doc:
        pix = page.get_pixmap(dpi=dpi)
        n += 1
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=pix.tobytes("jpg", jpg_quality=jpg_quality))
    out.save(str(path), deflate=True, garbage=4)
    out.close()
    src_doc.close()
    return n


def build_txt(path: Path, chapters: int, rnd: random.Random, paras: int = 30) -> None:
    parts = [_chapter_text(rnd, ci, paras) for ci in range(1, chapters + 1)]
    path.write_text("\n\n".join(parts), encoding="utf-8")


def build_epub(path: Path, chapters: int, rnd: random.Random, paras: int = 30) -> None:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("bench-epub-0001")
    book.set_title(f"{SUBJECT}（基准测试本）")
    book.set_language("zh")
    book.add_author("bench")
    spine = ["nav"]
    for ci in range(1, chapters + 1):
        ch = epub.EpubHtml(
            title=f"第{ci}章", file_name=f"chap_{ci}.xhtml", lang="zh",
        )
        body = "".join(
            f"<p>{ln}</p>" for ln in _chapter_text(rnd, ci, paras).split("\n") if ln
        )
        ch.content = f"<html><body><h1>第{ci}章</h1>{body}</body></html>"
        book.add_item(ch)
        spine.append(ch)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(str(path), book)


def build_docx(path: Path, chapters: int, rnd: random.Random, paras: int = 30) -> None:
    import docx

    d = docx.Document()
    for ci in range(1, chapters + 1):
        d.add_heading(f"第{ci}章", level=1)
        for ln in _chapter_text(rnd, ci, paras).split("\n"):
            if ln:
                d.add_paragraph(ln)
    d.save(str(path))


def build_scan_tree(root: Path, n_files: int, depth: int = 3) -> int:
    """生成用于 scan 基准的目录树：depth 层嵌套、n_files 个受支持文件。"""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    made = 0
    per_dir = max(1, n_files // (depth * 4))
    for a in range(4):
        for b in range(depth):
            d = root / f"分类{a}" / f"子类{b}"
            d.mkdir(parents=True, exist_ok=True)
            for i in range(per_dir):
                (d / f"book_{a}_{b}_{i}.txt").write_text(
                    f"第1章 样本\n内容 {a}-{b}-{i} " + "填充" * 50,
                    encoding="utf-8",
                )
                made += 1
                if made >= n_files:
                    return made
            # 混入被排除 / 不支持的文件，模拟真实书库
            (d / f"noise_{a}_{b}.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 512)
            (d / f"zip_{a}_{b}.zip").write_bytes(b"PK\x03\x04" + b"0" * 512)
    return made


def main() -> int:
    FIX.mkdir(parents=True, exist_ok=True)
    _env.CACHE.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(20261020)

    n = build_scan_tree(FIX / "scan_tree", 5000)
    print(f"[scan_tree]    {n} 个 txt（含 jpg/zip 噪声）")

    pages = build_text_pdf(FIX / "text_book.pdf", chapters=20, rnd=rnd)
    print(f"[text_book]    {pages} 页 · {(FIX/'text_book.pdf').stat().st_size/1024:.0f} KB")  # noqa: E501

    spages = build_scanned_pdf(FIX / "scanned_book.pdf", FIX / "text_book.pdf")
    print(f"[scanned_book] {spages} 页 · "
          f"{(FIX/'scanned_book.pdf').stat().st_size/1024/1024:.1f} MB")

    build_txt(FIX / "plain.txt", chapters=12, rnd=rnd)
    print(f"[plain.txt]    {(FIX/'plain.txt').stat().st_size/1024:.0f} KB")

    # GBK 编码的大文本：老实现会对非 UTF-8 文件反复整本读取，这个文件专门放大它
    gbk = "\n\n".join(_chapter_text(random.Random(99 + c), c, 30) for c in range(1, 21))
    (FIX / "gbk_book.txt").write_bytes(gbk.encode("gbk"))
    print(f"[gbk_book.txt] {len(gbk)} 字 · "
          f"{(FIX/'gbk_book.txt').stat().st_size/1024:.0f} KB（GBK 编码）")

    build_epub(FIX / "book.epub", chapters=12, rnd=rnd)
    print(f"[book.epub]    {(FIX/'book.epub').stat().st_size/1024:.0f} KB")

    build_docx(FIX / "book.docx", chapters=12, rnd=rnd)
    print(f"[book.docx]    {(FIX/'book.docx').stat().st_size/1024:.0f} KB")

    # 大文本：模拟长篇，用来放大 _split_chapters / chunk 的开销
    big = "\n\n".join(_chapter_text(random.Random(7 + c), c, 60) for c in range(1, 41))
    (FIX / "big.txt").write_text(big, encoding="utf-8")
    print(f"[big.txt]      {len(big)} 字 · {(FIX/'big.txt').stat().st_size/1024:.0f} KB")
    print("\n语料就绪：", FIX)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
