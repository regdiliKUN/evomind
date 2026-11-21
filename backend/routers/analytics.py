"""学习分析：总览快照、检索解释、学习建议、健康度、事实质量、证据策略、获取效率与缓存性能。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import memory

router = APIRouter()


# --------------------------------------------------------------------------
# 学习分析（增强可视化用）
# --------------------------------------------------------------------------
@router.get("/api/analytics")
def api_analytics() -> dict:
    from .. import analytics

    return analytics.snapshot()


@router.get("/api/analytics/search")
def api_analytics_search(q: str = Query(...), k: int = Query(8, ge=1, le=30)) -> dict:
    from .. import analytics

    return analytics.search_explain(q, k=min(max(1, int(k)), 30))


@router.get("/api/analytics/suggestions")
def api_analytics_suggestions() -> dict:
    """学习建议（引导）：下一步做什么，供总览「下一步建议」卡片使用。"""
    from .. import analytics

    return {"items": analytics.suggestions()}


@router.get("/api/analytics/health")
def api_analytics_health() -> dict:
    """知识库健康度：总分 + 四个维度短板 + 最薄弱项，供总览健康度卡片使用。"""
    from .. import analytics

    return analytics.health()


@router.get("/api/analytics/quality")
def api_analytics_quality() -> dict:
    """事实质量指标：证据覆盖、独立核查、纠错、负反馈、来源失效。

    与模型自报 confidence 分列——后者在生产库里几乎恒为 0.92，
    不能用来判断知识对错。
    """
    from .. import analytics

    return analytics.quality_snapshot()


@router.get("/api/analytics/evidence-policy")
def api_evidence_policy() -> dict:
    """证据策略当前生效状态与拒绝统计。

    用途：把 require_evidence 从 False 切到 True 之前，先用
    require_evidence_paths + require_evidence_dry_run 观察一段时间，
    看清楚会被拒掉多少条、集中在哪条入库通路，而不是一刀切。
    """
    from .. import config, evidence

    vm = str(config.get("verify_model", "") or "")
    primary = str(config.get("model", "") or "")
    return {
        "require_evidence": bool(config.get("require_evidence", False)),
        "require_evidence_paths": list(config.get("require_evidence_paths") or []),
        "require_evidence_dry_run": bool(config.get("require_evidence_dry_run", False)),
        "verify_model": vm,
        "primary_model": primary,
        "independent_verification": bool(vm and vm != primary),
        "rejects": evidence.reject_stats(),
        "path_diagnostics": evidence.policy_diagnostics(),
    }


@router.get("/api/analytics/acquisition")
def api_analytics_acquisition(limit: int = 10) -> dict:
    """知识获取效率：读过多少篇、产出率、各方向的"费料"程度。

    配合 /api/status 里的 engine.funnel（本轮各环节数量）一起看：
    funnel 说明"这一轮丢在哪一步"，这里说明"长期看哪些方向在读白书"。
    """
    return memory.acquisition_report(limit=limit)


@router.get("/api/perf")
def api_perf() -> dict:
    """检索/聚合缓存的命中率与平均耗时，供分析视图的「检索性能」卡片展示。"""
    return memory.perf_stats()
