"""扫描件 OCR：把图片型书籍变成可学习的语料。

双引擎设计（实测数据）：
  - rapid：RapidOCR(ONNXRuntime)，约 5 秒/页，纯文字识别，适合批量跑
  - vision：本地视觉模型（qwen3.8:27b），约 30-60 秒/页，
            能理解表格与版面，质量明显更高，适合精修关键页

流程：逐页渲染 → 识别 → 存 ocr_pages → 可校对 → 拼成章节 → 走 library 的提炼入库
支持断点续做：已识别的页会跳过，中断后重跑不会重复消耗时间。
"""
from __future__ import annotations

import base64
import io
import re
import threading
import time
from typing import Any, Callable

import httpx
import pymupdf

from . import config, memory

Event = Callable[[str, str, dict], None]

DEFAULT_DPI = 200          # 200 足够识别正文，再高只是拖慢
MAX_PIXELS = 2600          # 长边上限，防止超大页拖垮识别
VISION_MODEL = "qwen3.8:27b"

_ocr_engine = None
_engine_lock = threading.Lock()

_task: dict[str, Any] = {
    "running": False,
    "book_id": "",
    "book_title": "",
    "engine": "rapid",
    "total": 0,
    "done": 0,
    "failed": 0,
    "page": 0,
    "chars": 0,
    "stage": "idle",
    "stop": False,
    "elapsed": 0.0,
    "log": [],
}
_task_lock = threading.RLock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT,
    page_no INTEGER,
    text TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    chars INTEGER DEFAULT 0,
    elapsed REAL DEFAULT 0,
    edited INTEGER DEFAULT 0,
    created_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_book_page ON ocr_pages(book_id, page_no);
"""


def init_db() -> None:
    c = memory.conn()
    c.executescript(SCHEMA)
    c.commit()


def _emit(on_event: Event | None, level: str, msg: str, **payload) -> None:
    with _task_lock:
        _task["log"].append({"ts": time.time(), "level": level, "msg": msg})
        _task["log"] = _task["log"][-200:]
    if on_event:
        on_event(level, msg, payload)


def state() -> dict:
    with _task_lock:
        d = {k: v for k, v in _task.items() if k != "log"}
        d["log"] = _task["log"][-25:]
        return d


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
def _rapid():
    global _ocr_engine
    with _engine_lock:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
        return _ocr_engine


def _pix_to_array(pix) -> Any:
    import numpy as np

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    return arr


def render_page(doc, page_no: int, dpi: int = DEFAULT_DPI) -> tuple[bytes, Any]:
    """渲染一页为 PNG 字节 + numpy 数组。"""
    page = doc[page_no]
    pix = page.get_pixmap(dpi=dpi)
    # 超大页降采样，避免识别时间爆炸
    long_edge = max(pix.width, pix.height)
    if long_edge > MAX_PIXELS:
        scale = MAX_PIXELS / long_edge
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale * dpi / 72, scale * dpi / 72))
    return pix.tobytes("png"), _pix_to_array(pix)


def ocr_rapid(img_array) -> str:
    """RapidOCR：按 y 坐标排序后合并成段落文本。"""
    ocr = _rapid()
    res, _ = ocr(img_array)
    if not res:
        return ""
    items = []
    for box, text, score in res:
        try:
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            items.append((sum(ys) / len(ys), sum(xs) / len(xs), str(text), float(score)))
        except Exception:
            continue
    if not items:
        return ""

    items.sort(key=lambda x: (x[0], x[1]))
    # 同一行（y 接近）的块合并，行间按间距判断是否分段
    lines: list[list[tuple]] = []
    for it in items:
        if lines and abs(it[0] - lines[-1][-1][0]) < 12:
            lines[-1].append(it)
        else:
            lines.append([it])

    out: list[str] = []
    prev_y = None
    for ln in lines:
        ln.sort(key=lambda x: x[1])
        text = "".join(x[2] for x in ln).strip()
        if not text:
            continue
        y = ln[0][0]
        if prev_y is not None and (y - prev_y) > 40:
            out.append("")  # 大间距视为分段
        out.append(text)
        prev_y = y
    return "\n".join(out)


VISION_PROMPT = (
    "识别这张图片中的所有文字，按阅读顺序输出纯文本。\n"
    "要求：\n"
    "- 保留段落与标题层级\n"
    "- 表格用 Markdown 表格输出\n"
    "- 只输出文字本身，不要描述图片、不要加评论\n"
    "- 无法辨认的字符用「□」代替\n"
    "- 图注、页码、章节号也要保留"
)


def ocr_vision(img_bytes: bytes, model: str = "") -> str:
    """用本地视觉模型识别，能理解版面与表格，但慢得多。"""
    b64 = base64.b64encode(img_bytes).decode()
    body = {
        "model": model or config.get("vision_model") or VISION_MODEL,
        "messages": [{"role": "user", "content": VISION_PROMPT, "images": [b64]}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1, "num_predict": 3000},
    }
    r = httpx.post(
        f"{config.get('ollama_host') or 'http://127.0.0.1:11434'}/api/chat",
        json=body,
        timeout=300,
    )
    if r.status_code != 200:
        raise RuntimeError(f"视觉模型返回 HTTP {r.status_code}")
    return (r.json().get("message", {}) or {}).get("content", "").strip()


def vision_available() -> tuple[bool, str]:
    """检查本地是否有支持视觉的模型。"""
    host = config.get("ollama_url") or "http://127.0.0.1:11434"
    name = config.get("vision_model") or VISION_MODEL
    try:
        r = httpx.post(f"{host}/api/show", json={"model": name}, timeout=8)
        if r.status_code != 200:
            return False, f"模型 {name} 不存在"
        caps = (r.json() or {}).get("capabilities") or []
        if "vision" not in caps:
            return False, f"{name} 不支持视觉"
        return True, name
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:80]


# --------------------------------------------------------------------------
# 页范围解析
# --------------------------------------------------------------------------
def parse_pages(spec: str, total: int) -> list[int]:
    """支持 "1-50"、"1,3,5"、"1-20,30-40"、"all"。页码从 1 开始。"""
    spec = (spec or "all").strip().lower()
    if spec in ("all", "", "*"):
        return list(range(total))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            a, b = min(a, b), max(a, b)
            pages.update(range(max(1, a) - 1, min(total, b)))
        elif part.isdigit():
            n = int(part) - 1
            if 0 <= n < total:
                pages.add(n)
    return sorted(pages)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def ocr_page(book_id: str, page_no: int, engine: str = "rapid", dpi: int = DEFAULT_DPI) -> dict:
    """识别单页并落库。已存在且未编辑的会覆盖。"""
    from . import library

    init_db()
    c = memory.conn()
    row = c.execute("SELECT path, title FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        return {"ok": False, "msg": "书目不存在"}
    doc = pymupdf.open(row["path"])
    if page_no >= doc.page_count:
        return {"ok": False, "msg": "页码超出范围"}

    t0 = time.time()
    img_bytes, arr = render_page(doc, page_no, dpi)
    if engine == "vision":
        text = ocr_vision(img_bytes)
    else:
        text = ocr_rapid(arr)
    elapsed = time.time() - t0

    c.execute(
        "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
        " VALUES (?,?,?,?,?,?,0,?)"
        " ON CONFLICT(book_id,page_no) DO UPDATE SET"
        " text=excluded.text, engine=excluded.engine, chars=excluded.chars,"
        " elapsed=excluded.elapsed, edited=0",
        (book_id, page_no, text, engine, len(text), elapsed, time.time()),
    )
    c.commit()
    return {"ok": True, "page": page_no, "chars": len(text), "elapsed": round(elapsed, 1), "text": text}


def ocr_book_async(
    book_id: str,
    *,
    pages: str = "all",
    engine: str = "rapid",
    dpi: int = DEFAULT_DPI,
    skip_done: bool = True,
    on_event: Event | None = None,
) -> dict:
    """后台批量识别一本书。"""
    with _task_lock:
        if _task["running"]:
            return {"ok": False, "msg": "已有 OCR 任务在跑，请先等待或停止"}

    # 书库导入和 OCR 都是重任务，同时跑会互相拖慢（都吃 CPU/GPU）
    try:
        from . import finetune as _ft, library as _lib

        if _lib.state().get("running"):
            return {"ok": False, "msg": "书库导入任务正在跑，等它结束再开始 OCR"}
        if _ft.state().get("running"):
            return {"ok": False, "msg": "微调训练正在跑，等它结束再开始 OCR"}
    except Exception:
        pass

    def go() -> None:
        from . import library

        init_db()
        c = memory.conn()
        row = c.execute("SELECT path, title FROM books WHERE id=?", (book_id,)).fetchone()
        if not row:
            _emit(on_event, "error", "书目不存在")
            return
        try:
            doc = pymupdf.open(row["path"])
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "error", f"打不开文件：{str(e)[:80]}")
            return

        total_pages = doc.page_count
        targets = parse_pages(pages, total_pages)
        if skip_done:
            done_rows = c.execute(
                "SELECT page_no FROM ocr_pages WHERE book_id=? AND chars>0", (book_id,)
            ).fetchall()
            done = {r["page_no"] for r in done_rows}
            targets = [p for p in targets if p not in done]

        with _task_lock:
            _task.update(
                running=True, book_id=book_id, book_title=row["title"],
                engine=engine, total=len(targets), done=0, failed=0,
                page=0, chars=0, stage="ocr", stop=False, elapsed=0.0,
            )
        _emit(on_event, "stage", f"开始 OCR：《{row['title']}》 共 {len(targets)} 页（引擎 {engine}）")

        t_all = time.time()
        for i, pno in enumerate(targets):
            if _task["stop"]:
                _emit(on_event, "warn", "收到停止指令，已中断（进度已保存，可续做）")
                break
            try:
                img_bytes, arr = render_page(doc, pno, dpi)
                t0 = time.time()
                if engine == "vision":
                    text = ocr_vision(img_bytes)
                else:
                    text = ocr_rapid(arr)
                el = time.time() - t0
                c.execute(
                    "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
                    " VALUES (?,?,?,?,?,?,0,?)"
                    " ON CONFLICT(book_id,page_no) DO UPDATE SET"
                    " text=excluded.text, engine=excluded.engine, chars=excluded.chars,"
                    " elapsed=excluded.elapsed, edited=0",
                    (book_id, pno, text, engine, len(text), el, time.time()),
                )
                c.commit()
                with _task_lock:
                    _task["done"] = i + 1
                    _task["page"] = pno + 1
                    _task["chars"] += len(text)
                    _task["elapsed"] = round(time.time() - t_all, 1)
                if (i + 1) % 5 == 0 or i == 0:
                    _emit(
                        on_event, "info",
                        f"第 {pno+1}/{total_pages} 页 · {len(text)} 字 · {el:.1f}s"
                        f"（{i+1}/{len(targets)}）",
                    )
            except Exception as e:  # noqa: BLE001
                with _task_lock:
                    _task["failed"] += 1
                    _task["done"] = i + 1
                _emit(on_event, "warn", f"第 {pno+1} 页识别失败：{str(e)[:70]}")

        # 汇总：把有文字的页拼成章节，让书目变成可导入状态
        rows = c.execute(
            "SELECT page_no, text FROM ocr_pages WHERE book_id=? AND chars>0 ORDER BY page_no",
            (book_id,),
        ).fetchall()
        total_chars = sum(len(r["text"]) for r in rows)
        avg = total_chars / max(1, len(rows))
        if total_chars > 200:
            c.execute("DELETE FROM book_chapters WHERE book_id=?", (book_id,))
            # 每 10 页合并为一章，便于分段提炼
            for idx, start in enumerate(range(0, len(rows), 10)):
                group = rows[start:start + 10]
                text = "\n\n".join(g["text"] for g in group)
                title = f"第 {group[0]['page_no']+1}-{group[-1]['page_no']+1} 页"
                c.execute(
                    "INSERT INTO book_chapters(book_id,idx,title,chars) VALUES (?,?,?,?)",
                    (book_id, idx, title, len(text)),
                )
            c.execute(
                "UPDATE books SET status='ready', chars=?, skip_reason='' WHERE id=?",
                (total_chars, book_id),
            )
            msg = f"识别完成：{len(rows)} 页有效，共 {total_chars} 字（平均 {avg:.0f} 字/页），已转为可导入状态"
            _emit(on_event, "ok", msg)
            # 字数明显偏低，说明引擎可能识别不足（日文、表格、复杂版面尤其明显）
            if avg < 150 and engine == "rapid":
                _emit(
                    on_event, "warn",
                    f"平均每页只有 {avg:.0f} 字，识别可能不完整。"
                    "这本书建议改用「精细引擎」重识别，质量会明显更好",
                )
        else:
            _emit(on_event, "warn", f"识别完成但有效文字只有 {total_chars} 字，可能是纯图册")
        c.commit()

        with _task_lock:
            _task.update(running=False, stage="idle")

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


def stop() -> dict:
    _task["stop"] = True
    return {"ok": True}


# --------------------------------------------------------------------------
# 查询与校对
# --------------------------------------------------------------------------
def list_pages(book_id: str) -> list[dict]:
    init_db()
    rows = memory.conn().execute(
        "SELECT page_no, chars, engine, elapsed, edited FROM ocr_pages"
        " WHERE book_id=? ORDER BY page_no",
        (book_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_page(book_id: str, page_no: int) -> dict:
    init_db()
    row = memory.conn().execute(
        "SELECT * FROM ocr_pages WHERE book_id=? AND page_no=?", (book_id, page_no)
    ).fetchone()
    return dict(row) if row else {}


def save_page(book_id: str, page_no: int, text: str) -> dict:
    """保存人工校对后的文本。"""
    init_db()
    c = memory.conn()
    c.execute(
        "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
        " VALUES (?,?,?,?,?,0,1,?)"
        " ON CONFLICT(book_id,page_no) DO UPDATE SET"
        " text=excluded.text, chars=excluded.chars, edited=1",
        (book_id, page_no, text, "manual", len(text), time.time()),
    )
    c.commit()
    return {"ok": True, "chars": len(text)}


def book_text(book_id: str, limit_pages: int = 0) -> str:
    """取出整本书的 OCR 文本，按页序拼接。"""
    init_db()
    sql = "SELECT page_no, text FROM ocr_pages WHERE book_id=? AND chars>0 ORDER BY page_no"
    rows = memory.conn().execute(sql, (book_id,)).fetchall()
    if limit_pages:
        rows = rows[:limit_pages]
    return "\n\n".join(r["text"] for r in rows)


def render_png(book_id: str, page_no: int, dpi: int = 110) -> bytes | None:
    """渲染页面为 PNG，供前端对照校对。"""
    row = memory.conn().execute("SELECT path FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        return None
    try:
        # 显式关闭：前端每翻一页都会调这里，不依赖 GC 回收文件句柄
        with pymupdf.open(row["path"]) as doc:
            if page_no >= doc.page_count:
                return None
            img, _ = render_page(doc, page_no, dpi)
            return img
    except Exception:
        return None


def ocr_stats(book_id: str) -> dict:
    init_db()
    c = memory.conn()
    r = c.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(chars),0) ch, COALESCE(SUM(edited),0) ed"
        " FROM ocr_pages WHERE book_id=?",
        (book_id,),
    ).fetchone()
    return {"pages": r["n"], "chars": r["ch"], "edited": r["ed"]}


def ocr_image_bytes(data: bytes, engine: str = "rapid") -> dict:
    """识别用户上传的单张图片（快速体验用）。"""
    import numpy as np

    t0 = time.time()
    if engine == "vision":
        text = ocr_vision(data)
    else:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        text = ocr_rapid(np.array(img))
    return {"ok": True, "text": text, "chars": len(text), "elapsed": round(time.time() - t0, 1)}
