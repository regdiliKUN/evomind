"""知识库：列表/检索/混合召回、词法索引、重复清理、知识治理（纠错/反馈/核查/来源复检）与详情。

注意注册顺序：所有 /api/knowledge/<字面量> 路由必须先于单段 /api/knowledge/{pid}，
否则 "search"、"cleanup"、"duplicates" 等会被当成知识点 id 吞掉。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import cleanup, memory

router = APIRouter()


# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------
@router.get("/api/knowledge")
def api_knowledge(
    topic: str | None = None, limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1000000), q: str | None = Query(None, max_length=200)
) -> dict:
    if q:
        items = memory.search(q, k=limit, topic=topic)
    else:
        items = memory.list_points(topic, limit, offset)
    topics = [{"topic": r[0]} for r in memory.conn().execute("SELECT DISTINCT topic FROM knowledge ORDER BY topic")]
    return {"items": items, "total": memory.count_points(topic), "topics": topics}


@router.delete("/api/knowledge/{pid}")
def api_knowledge_delete(pid: str, hard: bool = False) -> dict:
    """默认软删除（archived，可恢复）；hard=true 才物理删除。"""
    ok = memory.delete_point(pid, hard=hard)
    return {"ok": bool(ok), "mode": "hard" if hard else "archived"}


@router.get("/api/knowledge/search")
def api_knowledge_search(q: str = Query(...), k: int = Query(8, ge=1, le=100)) -> list[dict]:
    return memory.search(q, k=k)


@router.get("/api/knowledge/hybrid-search")
def api_knowledge_hybrid_search(
    q: str = Query(...), k: int = Query(8, ge=1, le=100), alpha: float = Query(0.5, ge=0., le=1.),
    topic: str | None = None,
) -> dict:
    """A3：向量 + FTS5/BM25 的 RRF 混合召回，返回每一路排名便于核对。"""
    from .. import search_index
    return search_index.hybrid_search(q, k=min(max(1, k), 100), topic=topic, alpha=alpha)


@router.get("/api/search/index")
def api_search_index_status() -> dict:
    from .. import search_index
    return search_index.status()


@router.post("/api/search/index/rebuild")
def api_search_index_rebuild() -> dict:
    from .. import search_index
    return search_index.rebuild()


@router.get("/api/knowledge/cleanup")
def api_cleanup_state() -> dict:
    return cleanup.state()


class CleanupRequest(BaseModel):
    threshold: float = Field(.9, ge=.5, le=1.)
    token: str
    topic: str | None = None


@router.get("/api/knowledge/cleanup/preview")
def api_cleanup_preview(threshold: float = Query(.9, ge=.5, le=1.), topic: str | None = None) -> dict:
    return cleanup.preview(threshold, topic)


@router.post("/api/knowledge/cleanup")
def api_cleanup(body: CleanupRequest | None = None) -> dict:
    return cleanup.clean(**body.model_dump()) if body else cleanup.clean()


@router.post("/api/knowledge/cleanup/undo")
def api_cleanup_undo() -> dict:
    return cleanup.undo()


@router.get("/api/knowledge/duplicates")
def api_knowledge_duplicates(
    threshold: float = Query(.90, ge=.5, le=1.), limit: int = Query(30, ge=1, le=200), topic: str | None = None, offset: int = Query(0, ge=0)
) -> dict:
    """近重复知识点对（0.85~0.92 那一带：很像但没被去重挡掉的）。

    声明位置同样必须在 /api/knowledge/{pid} 之前，否则 "duplicates" 会被当 id。
    """
    return memory.near_duplicates(threshold=threshold, limit=limit, topic=topic, offset=offset)


# --------------------------------------------------------------------------
# 知识治理：纠错 / 反馈 / 状态机 / 独立核查 / 来源复检
# 评审里最刺眼的缺口之一就是「发现一条知识错了只能删掉」——下面这组接口
# 让修正、隔离、复核都变成可留痕、可统计的动作，而不是不可逆的 DELETE。
# 所有子路径都是两段，必须在单段 {pid} 详情路由之前声明，避免被当成 id。
# --------------------------------------------------------------------------
class KnowledgePatch(BaseModel):
    claim: str | None = Field(default=None, max_length=2000)
    detail: str | None = Field(default=None, max_length=8000)
    evidence: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, max_length=32)
    reason: str = Field(default="", max_length=500)
    actor: str = Field(default="user", max_length=80)


class FeedbackRequest(BaseModel):
    verdict: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=1000)
    actor: str = Field(default="user", max_length=80)


class SourceCheckRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    status: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=500)


@router.patch("/api/knowledge/{pid}")
def api_knowledge_patch(pid: str, body: KnowledgePatch) -> dict:
    """改文本走 revise_point（旧版本进 revisions，状态回到 unverified）；
    只改状态走 set_status（非法迁移会被状态机拒绝）。"""
    from .. import governance

    if memory.get_point(pid) is None:
        raise HTTPException(404, "知识点不存在")
    try:
        changed = any(v is not None for v in (body.claim, body.detail, body.evidence))
        out = (
            governance.revise_point(
                pid, claim=body.claim, detail=body.detail, ev=body.evidence,
                reason=body.reason, actor=body.actor,
            )
            if changed else {"ok": True, "id": pid}
        )
        if body.status:
            out = dict(out) | governance.set_status(
                pid, body.status, reason=body.reason, actor=body.actor
            )
        return out
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/knowledge/{pid}/feedback")
def api_knowledge_feedback(pid: str, body: FeedbackRequest) -> dict:
    """负反馈（wrong/outdated/source_dead/duplicate）会把条目隔离为 disputed。"""
    from .. import governance

    if memory.get_point(pid) is None:
        raise HTTPException(404, "知识点不存在")
    try:
        return governance.add_feedback(pid, body.verdict, note=body.note, actor=body.actor)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/knowledge/{pid}/verify")
def api_knowledge_verify(pid: str) -> dict:
    """用配置的独立模型复核单条知识。

    没配 verify_model（或配成和主模型同一个）时如实返回 independent=False，
    绝不把「同一个模型再问一次」包装成独立核查。
    """
    from .. import governance

    p = memory.get_point(pid)
    if p is None:
        raise HTTPException(404, "知识点不存在")
    result = governance.independent_verify([p])
    if not result.get("independent"):
        governance.record_verification(pid, "self_verify", "skipped",
                                       detail=str(result.get("reason", "")))
        return {"ok": True, "independent": False,
                "reason": result.get("reason", ""),
                "hint": "把 verify_model 配成与 model 不同的模型后才能做独立核查"}
    passed = bool(result.get("verified"))
    governance.record_verification(
        pid, "independent_model", "pass" if passed else "fail",
        model=str(result.get("model", "")), detail=str(result.get("reason", "")),
    )
    return {"ok": True, "independent": True, "model": result.get("model", ""),
            "verdict": "pass" if passed else "fail", "reason": result.get("reason", "")}


@router.get("/api/knowledge/{pid}/revisions")
def api_knowledge_revisions(pid: str, limit: int = Query(20, ge=1, le=200)) -> dict:
    from .. import governance

    return {"items": governance.revisions_for(pid, limit)}


@router.get("/api/knowledge/{pid}/feedback")
def api_knowledge_feedback_list(pid: str, limit: int = Query(20, ge=1, le=200)) -> dict:
    from .. import governance

    return {"items": governance.feedback_for(pid, limit)}


@router.post("/api/sources/check")
def api_source_check(body: SourceCheckRequest) -> dict:
    """登记一次来源存活复检，供 source_failure_rate 指标使用。"""
    from .. import governance

    try:
        return governance.source_check(body.url, body.status, note=body.note)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/knowledge/{pid}")
def api_knowledge_detail(pid: str, k: int = 5) -> dict:
    """单条知识点详情 + 语义最近邻，供知识库详情面板使用。

    必须声明在 /api/knowledge/search 之后：FastAPI 按声明顺序匹配，
    否则 "search" 会被当成 {pid} 吞掉。
    """
    item = memory.get_point(pid)
    if item is None:
        raise HTTPException(404, "知识点不存在")
    return {"item": item, "similar": memory.similar_points(pid, k=k)}
