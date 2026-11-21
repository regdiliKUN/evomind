"""本地书库：目录管理、扫描、书目列表/详情、解析与导入。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

from .. import library, tasks
from ._common import _num, _text, _ids, _lib_evt

router = APIRouter()


@router.get("/api/library/stats")
def api_lib_stats() -> dict:
    return {"stats": library.library_stats(), "task": library.state()}


@router.get("/api/library/dirs")
def api_lib_dirs() -> list[dict]:
    return library.list_dirs()


@router.post("/api/library/dirs")
def api_lib_add_dir(body: dict = Body(...)) -> dict:
    path = _text(body.get("path"), limit=1000).strip('"')
    if not path:
        raise HTTPException(400, "缺少目录路径")
    if not Path(path).is_dir():
        raise HTTPException(400, "目录不存在或不是文件夹")
    excludes = body.get("excludes")
    if isinstance(excludes, list):
        excludes = [str(x)[:200] for x in excludes[:50] if x]
    else:
        excludes = None
    return library.add_dir(path, _text(body.get("label"), limit=120), excludes)


@router.patch("/api/library/dirs/{did}")
def api_lib_update_dir(did: str, body: dict = Body(...)) -> dict:
    # 原来 **body 把任意键透传给 update_dir，而后者用 f-string 拼 UPDATE 的
    # 列名集合。虽然库层已有白名单，接口层仍应收紧为显式契约。
    clean = {k: body[k] for k in ("label", "enabled", "excludes") if k in body}
    if "label" in clean:
        clean["label"] = _text(clean["label"], limit=120)
    if "excludes" in clean:
        ex = clean["excludes"]
        clean["excludes"] = ([str(x)[:200] for x in ex[:50] if x]
                             if isinstance(ex, list) else [])
    return library.update_dir(did, **clean)


@router.delete("/api/library/dirs/{did}")
def api_lib_del_dir(did: str, purge: bool = False) -> dict:
    return library.remove_dir(did, purge)


@router.post("/api/library/scan")
def api_lib_scan(body: dict = Body(default={})) -> dict:
    dirs = (body or {}).get("dirs") or None
    return library.scan_async(dirs, on_event=_lib_evt)


@router.get("/api/library/books")
def api_lib_books(
    status: str | None = None,
    dir_id: str | None = None,
    fmt: str | None = None,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=1000000),
) -> dict:
    return library.list_books(
        status=status, dir_id=dir_id, fmt=fmt, q=q, limit=limit, offset=offset
    )


@router.get("/api/library/books/{bid}")
def api_lib_book(bid: str) -> dict:
    d = library.book_detail(bid)
    if not d:
        raise HTTPException(404, "书目不存在")
    return d


@router.post("/api/library/books/{bid}/parse", name="api_lib_parse")
@tasks.exclusive("书目解析")
def api_lib_parse_one(bid: str) -> dict:
    return library.parse_book(bid, on_event=_lib_evt)


@router.post("/api/library/import")
def api_lib_import(body: dict = Body(...)) -> dict:
    ids = _ids(body.get("book_ids"))
    if not ids:
        raise HTTPException(400, "请选择要导入的书")
    return library.import_books_async(
        ids,
        topic=_text(body.get("topic"), limit=200),
        max_chunks=_num(body.get("max_chunks"), library.MAX_CHUNKS_PER_BOOK, 1, 500),
        on_event=_lib_evt,
    )


@router.post("/api/library/parse")
def api_lib_parse(body: dict = Body(...)) -> dict:
    """批量解析：只提取正文、标出扫描版，不做 LLM 提炼。"""
    ids = _ids(body.get("book_ids"))
    if not ids:
        raise HTTPException(400, "请选择要解析的书")
    return library.parse_books_async(ids, on_event=_lib_evt)


@router.post("/api/library/import-url")
def api_lib_import_url(body: dict = Body(...)) -> dict:
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少链接")
    return library.import_url(url, (body.get("topic") or "").strip(), on_event=_lib_evt)


@router.get("/api/library/state")
def api_lib_state() -> dict:
    return library.state()


@router.post("/api/library/stop")
def api_lib_stop() -> dict:
    return library.stop_task()


@router.delete("/api/library/books/{bid}")
def api_lib_del_book(bid: str, purge_points: bool = False) -> dict:
    return library.delete_book(bid, purge_points)
