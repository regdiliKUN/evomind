"""关键词知识总结：把已学到的、与某个概念相关的知识提炼成结构化总结（流式进度 + 历史记录）。"""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import digest

router = APIRouter()


class DigestRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=60)
    topic: str | None = Field(default=None, max_length=200)
    include_legacy: bool = False
    max_points: int = Field(120, ge=20, le=300)


@router.post("/api/digest/stream")
def api_digest_stream(body: DigestRequest) -> StreamingResponse:
    """SSE：每个阶段推一条 {stage, progress, msg}，最后一条 stage=done 携带完整总结记录。"""

    def gen():
        try:
            for evt in digest.run(body.keyword, topic=body.topic or None,
                                  include_legacy=body.include_legacy, max_points=body.max_points):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'stage': 'error', 'msg': f'总结失败：{e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/digests")
def api_digests(limit: int = Query(50, ge=1, le=200)) -> dict:
    return {"items": digest.list_digests(limit)}


@router.get("/api/digests/{did}/markdown")
def api_digest_markdown(did: str) -> PlainTextResponse:
    d = digest.get(did)
    if d is None:
        raise HTTPException(404, "总结不存在")
    name = quote(f"{d['keyword']}-知识总结.md")
    return PlainTextResponse(digest.to_markdown(d), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@router.get("/api/digests/{did}")
def api_digest_get(did: str) -> dict:
    d = digest.get(did)
    if d is None:
        raise HTTPException(404, "总结不存在")
    return d


@router.delete("/api/digests/{did}")
def api_digest_delete(did: str) -> dict:
    return {"ok": digest.delete(did)}
