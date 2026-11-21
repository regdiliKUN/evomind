"""事件流：SSE 实时推送、最近事件、按天聚合、告警与归档。"""
from __future__ import annotations

import json
import queue
import time

from fastapi import APIRouter, Body, Query
from fastapi.responses import StreamingResponse

from .. import evolve, memory
from ._common import _num, _flag

router = APIRouter()


# --------------------------------------------------------------------------
# 事件流
# --------------------------------------------------------------------------
@router.get("/api/events")
def api_events() -> StreamingResponse:
    q: queue.Queue = queue.Queue(maxsize=500)

    dropped = 0

    def listener(evt: dict) -> None:
        nonlocal dropped
        try:
            q.put_nowait(evt)
        except queue.Full:
            # 原来静默丢弃：慢客户端会以为事件流是完整的。现在丢掉最旧一条，
            # 换进一条明确告警，让界面知道有事件缺失。
            dropped += 1
            try:
                q.get_nowait()
                q.put_nowait({"level": "warn", "ts": time.time(),
                              "msg": f"事件缓冲已满，已丢弃 {dropped} 条实时事件"})
            except (queue.Empty, queue.Full):
                pass

    evolve.bus.subscribe(listener)

    def gen():
        try:
            yield "retry: 3000\n\n"
            for evt in evolve.bus.history[-40:]:
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            # 标记历史回放结束，前端据此区分「历史」和「实时」，
            # 避免重连时把历史事件重播一遍入场动画
            yield "event: history-end\ndata: {}\n\n"
            while True:
                try:
                    evt = q.get(timeout=5)  # 缩短到 5s：断线订阅更快被 finally 清理
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            evolve.bus.unsubscribe(listener)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/events/recent")
def api_events_recent(limit: int = Query(80, ge=1, le=1000)) -> list[dict]:
    return list(reversed(memory.recent_events(limit)))


@router.get("/api/events/rollups")
def api_events_rollups(days: int = Query(7, ge=1, le=3650)) -> dict:
    """A6：按天/级别聚合的事件计数 + 当前台账规模。"""
    from .. import event_ops
    return dict(event_ops.rollups(days=days), status=event_ops.ops_status())


@router.get("/api/events/alerts")
def api_events_alerts(limit: int = Query(20, ge=1, le=200)) -> dict:
    from .. import event_ops
    return {"items": event_ops.alerts(limit=limit)}


@router.post("/api/events/archive")
def api_events_archive(body: dict = Body(default={})) -> dict:
    """A6：默认 dry_run，只报数；显式 {"dry_run": false} 才无损搬运。"""
    from .. import event_ops
    return event_ops.archive(
        dry_run=_flag(body.get("dry_run", True)),
        keep_days=_num(body.get("keep_days"), 30, 1, 3650),
        batch=_num(body.get("batch"), event_ops.ARCHIVE_BATCH, 1, 5000),
    )
