"""多模态摄取（图片 / 音频 / 视频 / PDF）：能力体检、媒体检索、探测、摄取、清缓存与补漏。"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from .. import config, evolve, httpc, media, search

router = APIRouter()


# --------------------------------------------------------------------------
# 多模态摄取（图片 / 音频 / 视频 / PDF）
# --------------------------------------------------------------------------
class MediaIngestRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    direction: str | None = Field(default=None, max_length=200)


class MediaSearchRequest(BaseModel):
    q: str = Field(min_length=2, max_length=200)
    kinds: list[str] = Field(default_factory=lambda: ["image", "audio", "video"], max_length=3)
    limit: int = Field(default=6, ge=1, le=24)


class MediaBackfillRequest(BaseModel):
    """不填 topic = 按知识点数量取前 N 个方向，逐个补媒体。"""

    topic: str | None = Field(default=None, max_length=200)
    per_topic: int = Field(default=2, ge=1, le=6)
    limit_topics: int = Field(default=6, ge=1, le=12)


@router.get("/api/media/status")
def api_media_status() -> dict:
    """能力体检：视觉模型、语音识别、字幕、PDF、抽帧各自能不能用。"""
    caps = media.capabilities()
    caps["state"] = evolve.engine.snapshot()
    return caps


@router.post("/api/media/search")
def api_media_search(body: MediaSearchRequest) -> dict:
    kinds = [k for k in body.kinds if k in ("image", "audio", "video")]
    if not kinds:
        raise HTTPException(400, "kinds 只能是 image / audio / video")
    items = search.search_media(body.q, kinds=kinds, limit=body.limit)
    return {"items": items, "count": len(items)}


@router.post("/api/media/probe")
def api_media_probe(body: dict = Body(...)) -> dict:
    """不下载，只判断一个 URL 会被当成什么类型、能不能处理。

    无扩展名的地址（CDN / 图片代理）必须看响应头才能判准，
    所以这里对 http 链接真发一次 HEAD（失败退 1 字节 GET）。
    """
    url = str(body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "只支持 http/https 链接")
    try:
        httpc.assert_public_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    info = media.probe(url)
    kind = info.get("kind") or media.kind_of(url)
    allowed = list(config.get("media_kinds") or [])
    return {
        "url": url,
        "kind": kind,
        "supported": kind in media.SUPPORTED_KINDS,
        "enabled": bool(config.get("media_enabled", True)) and kind in allowed,
        "content_type": info.get("content_type", ""),
        "size_mb": round((info.get("size") or 0) / 1048576, 2),
        "hint": "" if kind in media.SUPPORTED_KINDS else "这是网页，将按正文抓取处理",
    }


@router.post("/api/media/ingest")
def api_media_ingest(body: MediaIngestRequest) -> dict:
    res = evolve.engine.ingest_media(body.urls, body.direction or "")
    if not res.get("ok"):
        raise HTTPException(409, res.get("msg", "无法启动摄取"))
    return res


@router.post("/api/media/clear")
def api_media_clear() -> dict:
    return media.clear_cache()


@router.post("/api/media/backfill")
def api_media_backfill(body: MediaBackfillRequest | None = Body(default=None)) -> dict:
    """用图片/音频/视频给已有知识补漏（只找媒体能补的缺口）。"""
    b = body or MediaBackfillRequest()
    res = evolve.engine.backfill_media(
        b.topic or "", per_topic=b.per_topic, limit_topics=b.limit_topics
    )
    if not res.get("ok"):
        raise HTTPException(409, res.get("msg", "无法启动补漏"))
    return res
