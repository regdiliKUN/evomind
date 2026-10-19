"""OCR 基准：把「渲染 / 识别 / 落库」三段拆开测，再跑一次端到端看吞吐。

    .venv/Scripts/python.exe scripts/bench/bench_ocr.py --stage
    .venv/Scripts/python.exe scripts/bench/bench_ocr.py --e2e --pages 1-40
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import _env  # noqa: F401

FIX = _env.FIXTURES


def _pages(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    return out


def stage(args) -> dict:
    import pymupdf

    from backend import ocr as ocrmod

    print("识别后端：", json.dumps(ocrmod.device_status(), ensure_ascii=False))
    pdf = FIX / "scanned_book.pdf"
    if not pdf.exists():
        print("缺少语料，先跑 scripts/bench/make_fixtures.py")
        return {}
    doc = pymupdf.open(str(pdf))
    pages = _pages(args.pages)
    print(f"\n===== 阶段拆分（{pdf.name}，{len(pages)} 页，DPI={args.dpi}）=====")

    res: dict[str, float] = {}
    rt_pix, rt_png, rt_arr, rt_infer, rt_write = [], [], [], [], []
    total_png = 0

    # 预热引擎（首次加载 ONNX 模型不计入）
    img0, arr0 = ocrmod.render_page(doc, pages[0], args.dpi)
    ocrmod.ocr_rapid(arr0)

    from backend import library, memory
    library.init_db()
    ocrmod.init_db()
    bid = "bench_ocr_book"
    c = memory.conn()
    c.execute(
        "INSERT OR REPLACE INTO books(id,path,title,status) VALUES (?,?,?,?)",
        (bid, str(pdf), "OCR 基准本", "ready"),
    )
    c.execute("DELETE FROM ocr_pages WHERE book_id=?", (bid,))
    c.commit()

    for pno in pages:
        page = doc[pno]

        t0 = time.perf_counter()
        pix = page.get_pixmap(dpi=args.dpi)
        t1 = time.perf_counter()
        png = pix.tobytes("png")
        t2 = time.perf_counter()
        arr = ocrmod._pix_to_array(pix)
        t3 = time.perf_counter()

        text = ocrmod.ocr_rapid(arr)
        t4 = time.perf_counter()

        c.execute(
            "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
            " VALUES (?,?,?,?,?,?,0,?)"
            " ON CONFLICT(book_id,page_no) DO UPDATE SET text=excluded.text,"
            " engine=excluded.engine, chars=excluded.chars, elapsed=excluded.elapsed, edited=0",
            (bid, pno, text, "rapid", len(text), 0.0, time.time()),
        )
        c.commit()
        t5 = time.perf_counter()

        rt_pix.append((t1 - t0) * 1000)
        rt_png.append((t2 - t1) * 1000)
        rt_arr.append((t3 - t2) * 1000)
        rt_infer.append((t4 - t3) * 1000)
        rt_write.append((t5 - t4) * 1000)
        total_png += len(png)
    doc.close()

    def row(name, xs):
        print(f"  {name:<26} 平均 {statistics.mean(xs):8.2f} ms · 中位 "
              f"{statistics.median(xs):8.2f} ms · 合计 {sum(xs):9.1f} ms")

    row("① 渲染位图 get_pixmap", rt_pix)
    row("② PNG 编码 tobytes('png')", rt_png)
    row("③ 转 numpy 数组", rt_arr)
    row("④ RapidOCR 识别", rt_infer)
    row("⑤ 写库 + commit", rt_write)
    total = sum(rt_pix) + sum(rt_png) + sum(rt_arr) + sum(rt_infer) + sum(rt_write)
    print(f"  {'合计':<26} {total:9.1f} ms · "
          f"{len(pages)/total*1000*60:.0f} 页/分钟（单线程串行）")
    print(f"  PNG 总字节 {total_png/1024/1024:.1f} MB（rapid 引擎其实用不到）")

    res.update({
        "render_pixmap_ms": statistics.mean(rt_pix),
        "render_png_ms": statistics.mean(rt_png),
        "to_array_ms": statistics.mean(rt_arr),
        "infer_ms": statistics.mean(rt_infer),
        "write_ms": statistics.mean(rt_write),
        "total_ms": total,
        "ppm_single_thread": len(pages) / total * 1000 * 60,
        "png_mb": total_png / 1024 / 1024,
    })

    # 大页渲染：验证「超限后二次渲染」的实际代价
    print("\n===== 超大页渲染（长边 > MAX_PIXELS 会渲染两次） =====")
    big = FIX / "big_page.pdf"
    if not big.exists():
        import pymupdf as _f
        d = _f.open()
        d.new_page(width=1190, height=1684)   # A3 纵向
        for pg in d:
            pg.insert_text((100, 800), "大页渲染基准 " * 20, fontsize=20, fontname="china-s")
        d.save(str(big), deflate=True)
        d.close()
    d2 = pymupdf.open(str(big))
    t0 = time.perf_counter()
    for _ in range(args.big_repeat):
        _, _a = ocrmod.render_page(d2, 0, args.dpi)
    el = (time.perf_counter() - t0) / args.big_repeat
    pg = d2[0]
    long_edge = max(pg.rect.width, pg.rect.height) * args.dpi / 72
    print(f"  页面长边 {long_edge:.0f}px（上限 {ocrmod.MAX_PIXELS}）· "
          f"render_page 平均 {el*1000:.1f} ms · 触发二次渲染：{long_edge > ocrmod.MAX_PIXELS}")
    res["big_page_render_ms"] = el * 1000
    res["big_page_long_edge"] = long_edge
    d2.close()
    return res


def e2e(args) -> dict:
    """跑真实的 ocr_book_async 流水线，测端到端吞吐。"""
    from backend import ocr as ocrmod

    print("识别后端：", json.dumps(ocrmod.device_status(), ensure_ascii=False))
    pdf = FIX / "scanned_book.pdf"
    from backend import library, memory
    library.init_db()
    ocrmod.init_db()
    bid = "bench_ocr_book"
    c = memory.conn()
    c.execute(
        "INSERT OR REPLACE INTO books(id,path,title,status) VALUES (?,?,?,?)",
        (bid, str(pdf), "OCR 基准本", "ready"),
    )
    c.execute("DELETE FROM ocr_pages WHERE book_id=?", (bid,))
    c.commit()

    pages = args.pages
    t0 = time.perf_counter()
    r = ocrmod.ocr_book_async(bid, pages=pages, engine=args.engine,
                              dpi=args.dpi, skip_done=False)
    if not r.get("ok"):
        print("启动失败：", r)
        return {}
    while True:
        s = ocrmod.state()
        if not s.get("running"):
            break
        time.sleep(0.4)
    # 线程收尾
    time.sleep(0.5)
    el = time.perf_counter() - t0
    st = ocrmod.state()
    done = st.get("done") or 0
    print(f"\n===== 端到端 =====")
    print(f"  页数 {done} · 耗时 {el:.1f}s · 吞吐 {done/el*60:.1f} 页/分钟")
    print(f"  状态：{ {k: st.get(k) for k in ('total','done','failed','chars','elapsed')} }")
    return {"e2e_pages": done, "e2e_sec": el, "e2e_ppm": done / el * 60 if el else 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--e2e", action="store_true")
    ap.add_argument("--pages", default="1-20")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--engine", default="rapid")
    ap.add_argument("--big-repeat", dest="big_repeat", type=int, default=3)
    args = ap.parse_args()

    _env.isolate()
    out: dict = {}
    if args.stage or not (args.stage or args.e2e):
        out.update(stage(args))
    if args.e2e:
        out.update(e2e(args))
    print("\nRESULT_JSON " + json.dumps({k: round(v, 4) for k, v in out.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
