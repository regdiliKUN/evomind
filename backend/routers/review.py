"""历史无引文知识复核流水线。

所有写动作默认 dry_run=True：接口天然只读，必须显式传 {"dry_run": false}
才会动库。plan() 走独立的 mode=ro 连接，不触发迁移，因此可以直接对
尚未升级的 256 MB 真实老库调用。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query

from ._common import _num, _flag

router = APIRouter()


@router.get("/api/review/plan")
def api_review_plan(sample: int = Query(6, ge=0, le=20)) -> dict:
    from .. import review_pipeline
    return review_pipeline.plan(sample=sample)


@router.post("/api/review/enqueue")
def api_review_enqueue(body: dict | None = Body(default=None)) -> dict:
    from .. import review_pipeline
    body = body or {}
    lim = body.get("limit")
    return review_pipeline.enqueue(dry_run=_flag(body.get("dry_run", True)),
                                   limit=_num(lim, 20, 1, 500) if lim is not None else None)


@router.post("/api/review/run-local")
def api_review_run_local(body: dict | None = Body(default=None)) -> dict:
    from .. import review_pipeline
    body = body or {}
    return review_pipeline.run_local(batch=_num(body.get("batch", 50), 50, 1, 500),
                                     dry_run=_flag(body.get("dry_run", True)),
                                     max_pages=_num(body.get("max_pages", 12), 12, 1, 200))


@router.post("/api/review/run-urls")
def api_review_run_urls(body: dict | None = Body(default=None)) -> dict:
    from .. import review_pipeline
    body = body or {}
    lim = body.get("limit")
    return review_pipeline.run_urls(
        limit=_num(lim, 20, 1, 500) if lim is not None else None,
        dry_run=_flag(body.get("dry_run", True)),
        timeout=_num(body.get("timeout", 6), 6.0, 0.5, 120.0, float))


@router.get("/api/review/status")
def api_review_status() -> dict:
    from .. import review_pipeline
    return review_pipeline.status()
