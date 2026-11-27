"""学习控制：启动/停止学习、随机方向、方向轮换、知识炼化、持续自学、会话列表。"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictBool, StrictInt

from .. import config, curriculum, directions, evolve, memory, scheduler, refinement, turbo

router = APIRouter()


class LearnRequest(BaseModel):
    direction: str = Field(min_length=1, max_length=1000)
    rounds: StrictInt | None = Field(default=None, ge=1, le=100)


class DirectionRequest(BaseModel):
    seed: str = Field(min_length=1, max_length=200)
    exclude: list[str] = Field(default_factory=list, max_length=30)


class RefineRequest(BaseModel):
    topic: str | None = Field(default=None, max_length=1000)


class AutoRequest(BaseModel):
    enabled: StrictBool
    direction: str | None = Field(default=None, max_length=1000)
    idle_seconds: StrictInt | None = Field(default=None, ge=1, le=30)
    idle_minutes: float | None = Field(default=None, ge=0, le=10080, allow_inf_nan=False)
    auto_rounds: StrictInt | None = Field(default=None, ge=1, le=100)


class RotationRequest(BaseModel):
    """模糊方向 + 三个可配置量；不填则沿用配置里的当前值。"""

    seed: str = Field(min_length=1, max_length=200)
    rounds_per_direction: StrictInt | None = Field(default=None, ge=1, le=2000)
    total_rounds: StrictInt | None = Field(default=None, ge=1, le=100000)
    subdirection_count: StrictInt | None = Field(default=None, ge=2, le=20)
    # 预览过、用户确认的子方向：带上就沿用，不再重新拆解
    items: list[str] | None = Field(default=None, max_length=20)


class RotationPlanRequest(BaseModel):
    seed: str = Field(min_length=1, max_length=200)
    count: StrictInt | None = Field(default=None, ge=2, le=20)


# --------------------------------------------------------------------------
# 学习
# --------------------------------------------------------------------------
@router.post("/api/learn/start")
def api_learn_start(body: LearnRequest) -> dict:
    direction = " ".join(body.direction.split())
    rounds = body.rounds
    if not direction:
        raise HTTPException(400, "请填写学习方向")
    res = evolve.engine.start(direction, rounds)
    if res.get("ok"):
        config.update({"last_direction": direction})
        memory.log_event("start", f"启动学习：{direction}", {"rounds": rounds})
    return res


@router.post("/api/learn/stop")
def api_learn_stop() -> dict:
    scheduler.disable()
    config.update({"continuous_learning": False})
    # 「停止」是全局急停：方向轮换也在用同一个学习引擎，不一起停会马上又被它拉起来
    res = curriculum.stop()
    evolve.engine.stop()
    return {"ok": True, "rotation": res}


@router.post("/api/learn/directions")
def api_directions(body: DirectionRequest) -> dict:
    try:
        return directions.refresh(body.seed, body.exclude)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


# --------------------------------------------------------------------------
# 学习方向自动更换（轮换）
# --------------------------------------------------------------------------
@router.get("/api/rotation")
def api_rotation() -> dict:
    return curriculum.state(include_last=True)


@router.post("/api/rotation/start")
def api_rotation_start(body: RotationRequest) -> dict:
    seed = " ".join(body.seed.split())
    if not seed:
        raise HTTPException(400, "请填写一个大致的学习方向")
    # 轮换自己会连续占用学习引擎，持续自学的定时器没有意义，先关掉
    scheduler.disable()
    config.update({"continuous_learning": False})
    res = curriculum.start(
        seed,
        rounds_per_direction=body.rounds_per_direction,
        total_rounds=body.total_rounds,
        subdirection_count=body.subdirection_count,
        items=body.items,
    )
    if res.get("ok"):
        memory.log_event("start", f"启动方向轮换：{seed}", {
            "rounds_per_direction": body.rounds_per_direction,
            "total_rounds": body.total_rounds,
        })
    return res


@router.post("/api/rotation/pause")
def api_rotation_pause() -> dict:
    return curriculum.pause()


@router.post("/api/rotation/skip")
def api_rotation_skip() -> dict:
    return curriculum.skip()


@router.post("/api/rotation/resume")
def api_rotation_resume() -> dict:
    return curriculum.resume()


@router.post("/api/rotation/stop")
def api_rotation_stop() -> dict:
    return curriculum.stop()


@router.get("/api/rotation/summary")
def api_rotation_summary(format: str = "json"):
    """成果汇总。format=text 直接返回可粘贴的纯文本，脚本与导出都方便。"""
    summ = curriculum.summary()
    text = curriculum.summary_text()
    if format == "text":
        return Response(text or "（本次没有学出成果）", media_type="text/plain; charset=utf-8")
    return {"summary": summ, "text": text, "history": curriculum.history(10)}


@router.post("/api/rotation/plan")
def api_rotation_plan(body: RotationPlanRequest) -> dict:
    try:
        return curriculum.preview(body.seed, body.count)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/api/refinements")
def api_refinements(topic: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    return {"items": refinement.history(topic, limit, offset), "total": refinement.history_count(topic), "state": refinement.state()}


@router.post("/api/refinements/start")
def api_refine_start(body: RefineRequest) -> dict:
    topic = body.topic.strip() if body.topic else ""
    return refinement.start(topic or None)


@router.post("/api/refinements/books/start")
def api_refine_books() -> dict:
    return refinement.start_books()


@router.post("/api/refinements/stop")
def api_refine_stop() -> dict:
    return refinement.stop()


# --------------------------------------------------------------------------
# 持续自学
# --------------------------------------------------------------------------
@router.get("/api/auto")
def api_auto_get() -> dict:
    return scheduler.state()


@router.post("/api/auto")
def api_auto_set(body: AutoRequest) -> dict:
    on = body.enabled
    direction = " ".join((body.direction or "").split()) or None
    patch = {}
    if body.idle_seconds is not None:
        patch["idle_seconds"] = body.idle_seconds
    elif body.idle_minutes is not None:
        patch["idle_seconds"] = max(1, min(30, int(body.idle_minutes * 60)))
    if body.auto_rounds is not None:
        patch["auto_rounds"] = body.auto_rounds
    if patch:
        config.update(patch)
    if on:
        if not (direction or config.get("last_direction")):
            raise HTTPException(400, "开启持续自学前，请先设定一个学习方向")
        config.update({"continuous_learning": True})
        return scheduler.enable(direction)
    config.update({"continuous_learning": False})
    return scheduler.disable()


# --------------------------------------------------------------------------
# 狂暴模式（挂机时吃满资源）
# --------------------------------------------------------------------------
@router.get("/api/turbo")
def api_turbo_get() -> dict:
    return turbo.state()


@router.post("/api/turbo")
def api_turbo_set(body: dict = Body(...)) -> dict:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(422, "enabled 必须是 true / false")
    return turbo.set_enabled(enabled)


@router.get("/api/sessions")
def api_sessions() -> list[dict]:
    return memory.list_sessions()
