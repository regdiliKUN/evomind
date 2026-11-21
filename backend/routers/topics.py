"""受控主题词表与别名（重分类只改 topic，不动 claim/detail/向量）。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query

router = APIRouter()


# --------------------------------------------------------------------------
# B2 · 受控主题词表与别名（重分类只改 topic，不动 claim/detail/向量）
# --------------------------------------------------------------------------
@router.get("/api/topics/clusters")
def api_topics_clusters(limit: int = Query(40, ge=1, le=200)) -> dict:
    from .. import topics
    return topics.clusters(limit=limit)


@router.get("/api/topics/vocabulary")
def api_topics_vocabulary() -> dict:
    from .. import topics
    return {"items": topics.vocabulary(), "aliases": topics.aliases()}


@router.post("/api/topics/vocabulary")
def api_topics_add_term(body: dict = Body(...)) -> dict:
    from .. import topics
    return topics.add_term(str(body.get("label") or ""),
                           parent=str(body.get("parent") or ""),
                           note=str(body.get("note") or ""))


@router.delete("/api/topics/vocabulary/{label}")
def api_topics_del_term(label: str) -> dict:
    from .. import topics
    return topics.remove_term(label)


@router.post("/api/topics/aliases")
def api_topics_add_alias(body: dict = Body(...)) -> dict:
    from .. import topics
    return topics.add_alias(str(body.get("alias") or ""),
                            str(body.get("canonical") or ""))


@router.delete("/api/topics/aliases/{alias}")
def api_topics_del_alias(alias: str) -> dict:
    from .. import topics
    return topics.remove_alias(alias)


@router.post("/api/topics/reclassify")
def api_topics_reclassify(body: dict = Body(...)) -> dict:
    """默认 dry_run：先报「会改多少条」，显式置 false 才真正执行。"""
    from .. import topics
    return topics.reclassify(
        body.get("raw_topics") or [], str(body.get("canonical") or ""),
        dry_run=bool(body.get("dry_run", True)),
        reason=str(body.get("reason") or ""),
    )
