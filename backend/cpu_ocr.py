"""狂暴模式 · CPU 后台 OCR：GPU 跑大模型学习的同时，用闲着的 CPU 识别扫描书。

书库里大量扫描版 PDF 没有文字层，导入时被标成「需 OCR」跳过。这个后台线程
在狂暴模式下逐本把它们识别出来，识别完就转为「可导入」，给学习补充语料。

设计要点：
* 只用 CPU（独立的 RapidOCR 实例，强制 CPUExecutionProvider），不碰显卡，
  不和 Ollama 抢显存；
* 不走 tasks.exclusive：它不算「重任务」，学习、轮换、书库导入照常跑；
* 4 个引擎 × 每个 5 线程。实测 13900K 上同一批 24 页：
  1×24 线程 63 页/分钟，2×12 107，4×6 128，6×4 131，8×3 129——
  拆成几个小引擎比一个大引擎快一倍，再多就不涨了；留出的核心给大模型和系统；
* 页级断点续做：每页识别完立即入库，关掉狂暴模式时做完手上这页就停，
  下次从没识别过的页继续；
* 识别结果和手动 OCR 写同一张 ocr_pages 表，收尾走 ocr.finalize_book，
  判定口径与手动「整书识别」完全一致。
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

from . import config, memory

WORKERS = 4            # 并行识别引擎数
THREADS_PER_ENGINE = 5  # 每个引擎的 ONNX 计算线程
IDLE_RECHECK = 300      # 没有待识别的书时，多久再查一次（秒）
NEED_OCR = "%需 OCR%"   # library 导入时给扫描版 PDF 写的 skip_reason 关键字

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_engines: list = []
_state: dict = {
    "running": False, "status": "idle", "book_id": "", "book_title": "",
    "done": 0, "total": 0, "books_done": 0, "image_only": 0, "pages_done": 0,
    "started_at": 0.0, "msg": "", "pending_books": None,
}

# --------------------------------------------------------------------------
# 运行记录：内存里的计数关掉狂暴模式就清零，早上想看「昨晚识别了多少」时
# 已经没了。每识别完一本就把本次运行的累计写盘，保留最近 30 次。
# --------------------------------------------------------------------------
RUNS_KEEP = 30
_runs_lock = threading.Lock()


def _runs_path() -> Path:
    return Path(config.DATA) / "cpu_ocr_runs.json"


def load_runs() -> list[dict]:
    try:
        data = json.loads(_runs_path().read_text(encoding="utf-8"))
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_run(run: dict) -> None:
    """按 started_at 找到本次运行的那条记录并覆盖，原子写盘。"""
    with _runs_lock:
        runs = [r for r in load_runs() if r.get("started_at") != run["started_at"]]
        runs = (runs + [run])[-RUNS_KEEP:]
        path = _runs_path()
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as f:
                tmp = Path(f.name)
                json.dump(runs, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            tmp = None
        except OSError:
            pass
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def _snapshot_run(ended: bool) -> dict:
    with _lock:
        return {
            "started_at": _state["started_at"],
            "ended_at": time.time() if ended else 0.0,
            "books": _state["books_done"] - _state["image_only"],
            "image_only": _state["image_only"],
            "pages": _state["pages_done"],
        }


def history() -> dict:
    """最近一次运行 + 历史累计（给界面「昨晚识别了多少」用）。"""
    runs = load_runs()
    return {
        "last_run": runs[-1] if runs else None,
        "totals": {
            "runs": len(runs),
            "books": sum(int(r.get("books", 0)) for r in runs),
            "pages": sum(int(r.get("pages", 0)) for r in runs),
        },
    }


def state() -> dict:
    with _lock:
        d = dict(_state)
    if d["running"] and d["started_at"] and d["pages_done"]:
        minutes = max(1e-6, (time.time() - d["started_at"]) / 60)
        d["pages_per_min"] = round(d["pages_done"] / minutes)
    else:
        d["pages_per_min"] = 0
    d.update(history())
    return d


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _stopped_by(ev: threading.Event) -> bool:
    from . import turbo

    return ev.is_set() or not turbo.cpu_ocr_wanted()


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
def _new_cpu_engine():
    """强制 CPU、限定线程数的 RapidOCR 实例（不复用 ocr._rapid 的单例：那个按配置可能走 GPU）。"""
    from . import ocr

    backend = ocr._rapid_backend()
    if not backend:
        raise RuntimeError("未安装 OCR 引擎（pip install rapidocr）")
    if backend == "rapidocr":
        from rapidocr import RapidOCR

        inner = RapidOCR(params={
            "EngineConfig.onnxruntime.intra_op_num_threads": THREADS_PER_ENGINE,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
        return ocr._Rapid2Compat(inner, "cpu", ["CPUExecutionProvider"])
    ocr._patch_rapidocr_prefix()
    from rapidocr_onnxruntime import RapidOCR

    base = {"det_use_cuda": False, "cls_use_cuda": False, "rec_use_cuda": False}
    try:
        return RapidOCR(**base, intra_op_num_threads=THREADS_PER_ENGINE, inter_op_num_threads=1)
    except TypeError:  # 老版本不认线程参数
        return RapidOCR(**base)


def _get_engines() -> list:
    if not _engines:
        _engines.extend(_new_cpu_engine() for _ in range(WORKERS))
    return _engines


# --------------------------------------------------------------------------
# 选书 / 识别一本
# --------------------------------------------------------------------------
def _count_pending(c) -> int:
    return c.execute(
        "SELECT COUNT(*) FROM books WHERE status='skipped' AND skip_reason LIKE ?", (NEED_OCR,)
    ).fetchone()[0]


def _pick(c):
    """页数少的先做：同样的时间能让更多本书变成可导入。"""
    from . import ocr

    busy = ""
    st = ocr.state()
    if st.get("running"):
        busy = st.get("book_id") or ""  # 用户手动在识别的那本让给它
    return c.execute(
        "SELECT id, path, title FROM books WHERE status='skipped' AND skip_reason LIKE ?"
        " AND id != ? ORDER BY COALESCE(NULLIF(pages, 0), 1000000), created_at LIMIT 1",
        (NEED_OCR, busy),
    ).fetchone()


def _mark(c, book_id: str, reason: str) -> None:
    """改掉「需 OCR」标记，免得下次又被挑中。"""
    c.execute("UPDATE books SET skip_reason=? WHERE id=?", (reason[:200], book_id))
    c.commit()


def _ocr_book(c, book_id: str, path: str, title: str, ev: threading.Event) -> str:
    """识别一本。返回 "ok"（转为可导入）/ "image"（几乎没字或打不开）/
    "stopped"（中途被停，进度已保存）。"""
    import pymupdf

    from . import ocr

    ocr.init_db()
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001
        _mark(c, book_id, f"CPU 后台 OCR 打不开文件：{str(e)[:80]}")
        memory.log_event("warn", f"CPU 后台 OCR 跳过《{title}》：打不开文件 · {str(e)[:60]}")
        return "image"
    try:
        # 识别过的页（哪怕是空白页）都不重做；手动 OCR 只跳过有字的页，这里更省
        seen = {r[0] for r in c.execute(
            "SELECT page_no FROM ocr_pages WHERE book_id=?", (book_id,)).fetchall()}
        targets = [p for p in range(doc.page_count) if p not in seen]
        _set(book_id=book_id, book_title=title, done=0, total=len(targets), status="ocr",
             msg=f"正在识别《{title}》")
        engines = _get_engines()
        q: queue.Queue = queue.Queue(maxsize=WORKERS * 2)
        lock = threading.Lock()
        counter = {"done": 0}

        def producer() -> None:
            # pymupdf 的 doc 不能多线程同时读，渲染集中在这一个线程里
            for pno in targets:
                if _stopped_by(ev):
                    break
                try:
                    arr = ocr.render_page(doc, pno, ocr.DEFAULT_DPI, need_png=False)[1]
                except Exception:  # noqa: BLE001
                    arr = None
                q.put((pno, arr))
            for _ in engines:
                q.put(None)

        def worker(engine) -> None:
            wc = memory.conn()
            while True:
                item = q.get()
                if item is None:
                    return
                pno, arr = item
                text, t0 = "", time.time()
                if arr is not None and not _stopped_by(ev):
                    try:
                        text = ocr.ocr_rapid(arr, engine)
                    except Exception:  # noqa: BLE001
                        text = ""
                elif arr is not None:
                    continue  # 停止信号已到：队列里剩下的页不识别，下次再做
                try:
                    # 与手动 OCR 同一条 upsert；人工改过的页（edited=1）不覆盖
                    wc.execute(
                        "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
                        " VALUES (?,?,?,?,?,?,0,?)"
                        " ON CONFLICT(book_id,page_no) DO UPDATE SET"
                        " text=excluded.text, engine=excluded.engine, chars=excluded.chars,"
                        " elapsed=excluded.elapsed, edited=0"
                        " WHERE ocr_pages.edited=0",
                        (book_id, pno, text, "rapid", len(text), time.time() - t0, time.time()),
                    )
                    wc.commit()
                except Exception:  # noqa: BLE001
                    continue
                with lock:
                    counter["done"] += 1
                    n = counter["done"]
                with _lock:
                    _state["done"] = n
                    _state["pages_done"] += 1

        prod = threading.Thread(target=producer, daemon=True, name="cpu-ocr-render")
        prod.start()
        ws = [threading.Thread(target=worker, args=(e,), daemon=True, name=f"cpu-ocr-{i}")
              for i, e in enumerate(engines)]
        for w in ws:
            w.start()
        prod.join()
        for w in ws:
            w.join()
        if _stopped_by(ev) and counter["done"] < len(targets):
            return "stopped"
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass

    total = ocr.finalize_book(c, book_id, "rapid", quiet=True)
    if total > 200:
        memory.log_event("ok", f"CPU 后台 OCR 完成《{title}》：{total} 字，已转为可导入")
        return "ok"
    _mark(c, book_id, f"OCR 后只有 {total} 字（可能是纯图册），已跳过")
    memory.log_event("info", f"CPU 后台 OCR：《{title}》几乎没有文字，可能是纯图册")
    return "image"


# --------------------------------------------------------------------------
# 后台线程
# --------------------------------------------------------------------------
def _loop(ev: threading.Event) -> None:
    c = memory.conn()
    _set(running=True, status="starting", started_at=time.time(), pages_done=0,
         books_done=0, image_only=0, msg="正在加载 CPU 识别引擎")
    _save_run(_snapshot_run(ended=False))
    memory.log_event("info", f"狂暴模式：CPU 后台 OCR 启动（{WORKERS} 引擎 × {THREADS_PER_ENGINE} 线程）")
    try:
        while not _stopped_by(ev):
            _set(pending_books=_count_pending(c))
            row = _pick(c)
            if row is None:
                _set(status="idle", book_id="", book_title="", done=0, total=0,
                     msg="书库里没有待识别的扫描书")
                ev.wait(IDLE_RECHECK)
                continue
            result = _ocr_book(c, row["id"], row["path"], row["title"], ev)
            if result != "stopped":
                with _lock:
                    _state["books_done"] += 1
                    if result == "image":
                        _state["image_only"] += 1
            _save_run(_snapshot_run(ended=False))
    except Exception as e:  # noqa: BLE001
        memory.log_event("error", f"CPU 后台 OCR 异常停止：{str(e)[:120]}")
        _set(msg=f"异常停止：{str(e)[:80]}")
    finally:
        _save_run(_snapshot_run(ended=True))
        with _lock:
            # 关了马上又开时，新线程已经接管：旧线程退出别把新线程的状态抹掉
            if _stop is ev:
                _state.update(running=False, status="idle", book_id="", book_title="",
                              done=0, total=0)


def start() -> None:
    global _thread, _stop
    with _lock:
        # 线程还活着但已收到停止信号（正在收尾）时，换一个新 Event 起新线程，
        # 否则「关了马上又开」会出现开关显示开着、后台却已退出。
        if _thread is not None and _thread.is_alive() and not _stop.is_set():
            return
        _stop = threading.Event()
        _thread = threading.Thread(target=_loop, args=(_stop,), daemon=True, name="cpu-ocr")
        _thread.start()


def stop(wait: float = 0) -> None:
    """发停止信号：各引擎做完手上那一页就停（进度已入库）。"""
    _stop.set()
    th = _thread
    if wait and th is not None:
        th.join(timeout=wait)
