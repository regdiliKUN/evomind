"""扫描件 OCR：任务状态、单本/批量识别、逐页结果与校对、页面渲染、单图识别。"""
from __future__ import annotations

from fastapi import APIRouter, Body, File, HTTPException, Path as FPath, Query, UploadFile
from fastapi.responses import Response

from .. import config, ocr
from ._common import _looks_like_image, _num, _text, _ids, _flag, _lib_evt

router = APIRouter()


MAX_OCR_IMAGE_BYTES = 30 * 1024 * 1024


# --------------------------------------------------------------------------
# 扫描件 OCR
# --------------------------------------------------------------------------
@router.get("/api/ocr/state")
def api_ocr_state() -> dict:
    return ocr.state()


@router.post("/api/ocr/stop")
def api_ocr_stop() -> dict:
    return ocr.stop()


@router.get("/api/ocr/vision")
def api_ocr_vision() -> dict:
    ok, info = ocr.vision_available()
    return {"available": ok, "model": info}


@router.get("/api/ocr/device")
def api_ocr_device() -> dict:
    """识别后端状态：GPU 是否真的在用、并发几线程。"""
    return ocr.device_status()


@router.post("/api/library/books/{bid}/ocr")
def api_book_ocr(bid: str, body: dict = Body(...)) -> dict:
    return ocr.ocr_book_async(
        bid,
        pages=_text(body.get("pages"), "all", 64),
        engine=_text(body.get("engine"), "rapid", 32),
        dpi=_num(body.get("dpi"), config.get("ocr_dpi", 200), 72, 600),
        skip_done=_flag(body.get("skip_done", True)),
        on_event=_lib_evt,
    )


@router.post("/api/ocr/batch")
def api_ocr_batch(body: dict = Body(...)) -> dict:
    """批量识别多本书：一本跑完再开下一本。"""
    return ocr.ocr_batch_async(
        _ids(body.get("book_ids")),
        pages=_text(body.get("pages"), "all", 64),
        engine=_text(body.get("engine"), "rapid", 32),
        dpi=_num(body.get("dpi"), config.get("ocr_dpi", 200), 72, 600),
        skip_done=_flag(body.get("skip_done", True)),
        on_event=_lib_evt,
    )


@router.get("/api/library/books/{bid}/ocr")
def api_book_ocr_pages(bid: str) -> dict:
    return {"pages": ocr.list_pages(bid), "stats": ocr.ocr_stats(bid)}


@router.get("/api/library/books/{bid}/ocr/{page_no}")
def api_book_ocr_page(bid: str, page_no: int) -> dict:
    d = ocr.get_page(bid, page_no)
    if not d:
        raise HTTPException(404, "这一页还没有识别结果")
    return d


@router.put("/api/library/books/{bid}/ocr/{page_no}")
def api_book_ocr_save(bid: str, page_no: int, body: dict = Body(...)) -> dict:
    return ocr.save_page(bid, page_no, body.get("text") or "")


@router.post("/api/library/books/{bid}/ocr/{page_no}/redo")
def api_book_ocr_redo(bid: str, page_no: int, body: dict = Body(default={})) -> dict:
    b = body or {}
    return ocr.ocr_page(
        bid, page_no,
        engine=_text(b.get("engine"), "rapid", 32),
        dpi=_num(b.get("dpi"), config.get("ocr_dpi", 200), 72, 600),
        # redo 是用户主动点「换引擎重识别」，显式授权覆盖人工校对稿。
        overwrite_edited=True,
    )


@router.get("/api/library/books/{bid}/page/{page_no}.png")
def api_book_page_png(bid: str, page_no: int = FPath(..., ge=1, le=100000),
                      dpi: int = Query(110, ge=36, le=300)) -> Response:
    data = ocr.render_png(bid, page_no, dpi)
    if not data:
        raise HTTPException(404, "页面渲染失败")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.post("/api/ocr/image")
async def api_ocr_image(
    file: UploadFile = File(...),
    engine: str = Query("rapid", pattern="^(rapid|vision|paddle|tesseract)$"),
) -> dict:
    """单图 OCR：读取走 async，识别下沉线程池，避免冻结事件循环。"""
    from starlette.concurrency import run_in_threadpool

    data = await file.read(MAX_OCR_IMAGE_BYTES + 1)
    if len(data) > MAX_OCR_IMAGE_BYTES:
        raise HTTPException(413, "图片超过 30MB")
    if not _looks_like_image(data):
        raise HTTPException(400, "文件不是支持的图片格式")
    return await run_in_threadpool(ocr.ocr_image_bytes, data, engine)
