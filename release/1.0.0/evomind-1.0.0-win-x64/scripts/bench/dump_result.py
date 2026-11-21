"""把「导入」和「OCR」的产物导成一份可比对的结果文件。

优化前后各跑一次，比对两份 JSON 是否一致 —— 这是「输出结果一致」的判据：
    .venv/Scripts/python.exe scripts/bench/dump_result.py --out before.json
    git stash push -- backend/            # 回到优化前
    .venv/Scripts/python.exe scripts/bench/dump_result.py --out before.json
    git stash pop
    .venv/Scripts/python.exe scripts/bench/dump_result.py --out after.json
    .venv/Scripts/python.exe scripts/bench/dump_result.py --cmp before.json after.json

导入部分用假 LLM（确定性），这样比对的是我们改动的那些代码路径本身，
而不是模型每次不同的输出。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import _env  # noqa: F401


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


def collect_import() -> dict:
    _env.fake_llm()
    from backend import library, memory

    library.init_db()
    c = memory.conn()
    out: dict = {"books": [], "knowledge": []}

    for name in ("plain.txt", "gbk_book.txt", "book.epub", "book.docx",
                 "text_book.pdf", "big.txt"):
        p = _env.FIXTURES / name
        if not p.exists():
            continue
        bid = hashlib.md5(str(p).encode("utf-8")).hexdigest()[:16]
        c.execute(
            "INSERT OR REPLACE INTO books(id,dir_id,path,source_type,format,title,"
            "size_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, "", str(p), "local", p.suffix.lstrip("."), p.stem,
             p.stat().st_size, "pending", time.time()),
        )
        c.commit()

        pr = library.parse_book(bid)
        row = c.execute(
            "SELECT title,chars,pages,text_ratio,status FROM books WHERE id=?", (bid,)
        ).fetchone()
        chapters = [
            {"idx": r["idx"], "title": r["title"], "chars": r["chars"]}
            for r in c.execute(
                "SELECT idx,title,chars FROM book_chapters WHERE book_id=? ORDER BY idx",
                (bid,),
            ).fetchall()
        ]
        imp = library.import_book(bid, topic="等价性核对", max_chunks=12)
        after = c.execute("SELECT status,points_count,topic FROM books WHERE id=?",
                          (bid,)).fetchone()
        out["books"].append({
            "file": name,
            "parse": {"ok": pr.get("ok"), "chars": pr.get("chars"),
                      "chapters": pr.get("chapters")},
            "row": dict(row),
            "chapters": chapters,
            "import": {"ok": imp.get("ok"), "points": imp.get("points"),
                       "chunks": imp.get("chunks")},
            "after": dict(after),
        })

    # 知识点：按 claim 排序，比对内容本身而不是插入顺序
    for r in c.execute(
        "SELECT topic,claim,detail,source_title,source_kind,tags,confidence,book_id"
        " FROM knowledge ORDER BY claim"
    ).fetchall():
        d = dict(r)
        out["knowledge"].append(d)
    out["knowledge_count"] = len(out["knowledge"])
    out["knowledge_sha"] = _sha(
        json.dumps(out["knowledge"], ensure_ascii=False, sort_keys=True)
    )
    return out


def collect_ocr(pages: str, dump_text: bool) -> dict:
    import pymupdf

    from backend import ocr as ocrmod

    pdf = _env.FIXTURES / "scanned_book.pdf"
    if not pdf.exists():
        return {"error": "缺少语料"}

    # 1) 渲染等价性：与「先渲染再按长边缩放」的老实现逐字节对比
    def old_render(doc, page_no, dpi):
        page = doc[page_no]
        pix = page.get_pixmap(dpi=dpi)
        long_edge = max(pix.width, pix.height)
        if long_edge > ocrmod.MAX_PIXELS:
            scale = ocrmod.MAX_PIXELS / long_edge
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(scale * dpi / 72, scale * dpi / 72)
            )
        return pix

    render_cmp = []
    doc = pymupdf.open(str(pdf))
    big = _env.FIXTURES / "big_page.pdf"
    probes = [(doc, i, "A4 扫描页") for i in (0, 1, 2, 37, 99)]
    if big.exists():
        probes.append((pymupdf.open(str(big)), 0, "A3 大页(超限)"))
    # 造几个尺寸，把阈值附近也覆盖到
    dsz = pymupdf.open()
    for w, h, nm in ((420, 595, "A5"), (595, 842, "A4"), (842, 1190, "A3"),
                     (1000, 1414, "B2"), (1190, 1684, "A2")):
        dsz.new_page(width=w, height=h)
    for i, (_, _, nm) in enumerate(((420, 595, "A5"), (595, 842, "A4"),
                                    (842, 1190, "A3"), (1000, 1414, "B2"),
                                    (1190, 1684, "A2"))):
        probes.append((dsz, i, f"{nm} 空白页"))

    for d, pno, label in probes:
        old = old_render(d, pno, 200)
        _png, arr = ocrmod.render_page(d, pno, 200)
        render_cmp.append({
            "label": label,
            "old_wh": [old.width, old.height],
            "new_wh": [int(arr.shape[1]), int(arr.shape[0])],
            "samples_sha": _sha(repr(old.samples)) if False else hashlib.sha256(
                old.samples
            ).hexdigest(),
            "array_sha": hashlib.sha256(arr.tobytes()).hexdigest(),
            "same": old.samples == arr.tobytes(),
        })
    doc.close()

    # 2) 识别结果等价性：逐页跑真实 OCR，记录文本哈希
    from backend import library, memory

    library.init_db()
    ocrmod.init_db()
    bid = "equiv_ocr_book"
    c = memory.conn()
    c.execute(
        "INSERT OR REPLACE INTO books(id,path,title,status) VALUES (?,?,?,?)",
        (bid, str(pdf), "等价性核对本", "ready"),
    )
    c.execute("DELETE FROM ocr_pages WHERE book_id=?", (bid,))
    c.commit()

    r = ocrmod.ocr_book_async(bid, pages=pages, engine="rapid", dpi=200,
                              skip_done=False)
    if not r.get("ok"):
        return {"error": "OCR 启动失败", "render_cmp": render_cmp}
    while ocrmod.state().get("running"):
        time.sleep(0.4)
    time.sleep(0.6)

    page_rows = []
    for row in c.execute(
        "SELECT page_no,chars,engine,edited FROM ocr_pages WHERE book_id=?"
        " ORDER BY page_no", (bid,)
    ).fetchall():
        d = dict(row)
        if dump_text:
            t = c.execute("SELECT text FROM ocr_pages WHERE book_id=? AND page_no=?",
                          (bid, d["page_no"])).fetchone()["text"]
            d["text_sha"] = _sha(t)
            d["text_len"] = len(t)
            d["text"] = t
        page_rows.append(d)
    chapters = [
        dict(r) for r in c.execute(
            "SELECT idx,title,chars FROM book_chapters WHERE book_id=? ORDER BY idx",
            (bid,),
        ).fetchall()
    ]
    book_row = dict(c.execute(
        "SELECT status,chars FROM books WHERE id=?", (bid,)).fetchone())
    return {
        "render_cmp": render_cmp,
        "pages": page_rows,
        "chapters": chapters,
        "book": book_row,
        "pages_sha": _sha(json.dumps(
            [{k: v for k, v in p.items() if k != "text"} for p in page_rows],
            ensure_ascii=False, sort_keys=True)),
    }


def compare(a: dict, b: dict) -> int:
    """比对两份结果，打印差异并给出退出码。"""
    ok = True

    def walk(path, x, y):
        nonlocal ok
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                walk(f"{path}.{k}", x.get(k), y.get(k))
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                print(f"  ✗ {path}: 长度不同 {len(x)} vs {len(y)}")
                ok = False
                return
            for i, (xi, yi) in enumerate(zip(x, y)):
                walk(f"{path}[{i}]", xi, yi)
        elif x != y:
            print(f"  ✗ {path}: {str(x)[:70]!r} != {str(y)[:70]!r}")
            ok = False

    walk("", a, b)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--cmp", nargs=2, metavar=("A", "B"), default=None)
    ap.add_argument("--pages", default="1-12", help="OCR 页范围")
    ap.add_argument("--with-text", action="store_true", help="导出 OCR 全文（便于人工看）")
    ap.add_argument("--skip-ocr", action="store_true")
    args = ap.parse_args()

    _env.isolate()

    if args.cmp:
        a = json.loads(Path(args.cmp[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.cmp[1]).read_text(encoding="utf-8"))
        print("=== 比对 ===")
        rc = compare(a, b)
        print("一致 ✓" if rc == 0 else "存在差异 ✗")
        return rc

    res: dict = {"import": collect_import()}
    if not args.skip_ocr:
        res["ocr"] = collect_ocr(args.pages, args.with_text)

    if args.out:
        Path(args.out).write_text(
            json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"已写入 {args.out}")
        print("  知识点条数:", res["import"]["knowledge_count"])
        print("  知识点指纹:", res["import"]["knowledge_sha"])
        if "ocr" in res:
            bad = [r for r in res["ocr"].get("render_cmp", []) if not r["same"]]
            print(f"  渲染逐字节一致: {len(res['ocr'].get('render_cmp', [])) - len(bad)}"
                  f"/{len(res['ocr'].get('render_cmp', []))}")
            print("  OCR 结果指纹:", res["ocr"].get("pages_sha"))
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
