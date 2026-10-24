"""EvoMind HTTP 服务：REST API + SSE 实时事件流 + 静态控制台。"""
from __future__ import annotations

import json
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt

from . import backup, cleanup, config, curriculum, directions, downloader, evolve, finetune, library, llm, media, memory, ocr, scheduler, refinement, search, tasks

app = FastAPI(title="EvoMind", version="1.0.0")
APP_REVISION = "20261023.1"

WEB = config.WEB
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


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


@app.exception_handler(Exception)
def _unhandled(request, exc: Exception) -> JSONResponse:
    """把后端异常转成前端能读的 JSON，而不是一片空白。"""
    import traceback

    memory.log_event("error", f"接口异常 {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc)[:400],
            "path": str(request.url.path),
            "trace": traceback.format_exc()[-1200:],
        },
    )


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
@app.get("/api/status")
def api_status() -> dict:
    # 仪表盘每几秒轮询一次。原来这里 healthy() + list_models() 向 Ollama 发两次
    # 完全相同的 /api/tags，加上 memory.stats 与 downloader.disk_report 各自
    # rglob 一遍 models 目录、finetune.check_env 每次探测 CUDA —— 一次轮询的
    # 固定开销全花在重复计算上。现在：tags() 一次往返（2s TTL）、disk_usage_gb
    # 5s TTL、check_env 30s TTL，轮询本身的开销降到一次轻量读取。
    t0 = time.perf_counter()
    ok, models = llm.tags()
    env = finetune.check_env()
    out = {
        "revision": APP_REVISION,
        "capabilities": ["book_refinement", "duplicate_cleanup", "semantic_cleanup_preview", "manual_random_direction", "direction_rotation"],
        "engine": evolve.engine.snapshot(),
        "rotation": curriculum.state(),
        "refinement": refinement.state(),
        "auto": scheduler.state(),
        "stats": memory.stats(),
        # checking=True 表示首次探测还没回来（Ollama 没启动时它会等满连接超时）。
        # 前端据此显示「检测中…」而不是直接判定「未连接」，避免第一帧误报。
        "llm": {"healthy": ok, "models": models, "checking": llm.tags_pending()},
        "config": config.load(),
        "disk": downloader.disk_report(),
        "finetune": {"ready": env["ready"], "gpu": env.get("gpu"), "cuda": env.get("cuda")},
        "finetune_state": finetune.state(),
        # 本次状态接口自身的端到端耗时（ms）。前端据此画一个延迟徽标，
        # 也正好是「优化有没有让轮询变快」的最直观证据（命中缓存后这项会明显降）。
        "response_ms": round((time.perf_counter() - t0) * 1000, 2),
    }
    memory.record_status_poll((time.perf_counter() - t0) * 1000)
    return out


@app.get("/api/config")
def api_config_get() -> dict:
    return config.load()


@app.post("/api/config")
def api_config_set(patch: dict = Body(...)) -> dict:
    allowed = set(config.DEFAULTS.keys())
    clean = {k: v for k, v in patch.items() if k in allowed}
    try:
        return config.update(clean)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


# --------------------------------------------------------------------------
# 学习
# --------------------------------------------------------------------------
@app.post("/api/learn/start")
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


@app.post("/api/learn/stop")
def api_learn_stop() -> dict:
    scheduler.disable()
    config.update({"continuous_learning": False})
    # 「停止」是全局急停：方向轮换也在用同一个学习引擎，不一起停会马上又被它拉起来
    res = curriculum.stop()
    evolve.engine.stop()
    return {"ok": True, "rotation": res}


@app.post("/api/learn/directions")
def api_directions(body: DirectionRequest) -> dict:
    try:
        return directions.refresh(body.seed, body.exclude)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


# --------------------------------------------------------------------------
# 学习方向自动更换（轮换）
# --------------------------------------------------------------------------
@app.get("/api/rotation")
def api_rotation() -> dict:
    return curriculum.state(include_last=True)


@app.post("/api/rotation/start")
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


@app.post("/api/rotation/pause")
def api_rotation_pause() -> dict:
    return curriculum.pause()


@app.post("/api/rotation/skip")
def api_rotation_skip() -> dict:
    return curriculum.skip()


@app.post("/api/rotation/resume")
def api_rotation_resume() -> dict:
    return curriculum.resume()


@app.post("/api/rotation/stop")
def api_rotation_stop() -> dict:
    return curriculum.stop()


@app.get("/api/rotation/summary")
def api_rotation_summary(format: str = "json"):
    """成果汇总。format=text 直接返回可粘贴的纯文本，脚本与导出都方便。"""
    summ = curriculum.summary()
    text = curriculum.summary_text()
    if format == "text":
        return Response(text or "（本次没有学出成果）", media_type="text/plain; charset=utf-8")
    return {"summary": summ, "text": text, "history": curriculum.history(10)}


@app.post("/api/rotation/plan")
def api_rotation_plan(body: RotationPlanRequest) -> dict:
    try:
        return curriculum.preview(body.seed, body.count)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/refinements")
def api_refinements(topic: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    return {"items": refinement.history(topic, limit, offset), "total": refinement.history_count(topic), "state": refinement.state()}


@app.post("/api/refinements/start")
def api_refine_start(body: RefineRequest) -> dict:
    topic = body.topic.strip() if body.topic else ""
    return refinement.start(topic or None)


@app.post("/api/refinements/books/start")
def api_refine_books() -> dict:
    return refinement.start_books()


@app.post("/api/refinements/stop")
def api_refine_stop() -> dict:
    return refinement.stop()


# --------------------------------------------------------------------------
# 持续自学
# --------------------------------------------------------------------------
@app.get("/api/auto")
def api_auto_get() -> dict:
    return scheduler.state()


@app.post("/api/auto")
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


@app.get("/api/sessions")
def api_sessions() -> list[dict]:
    return memory.list_sessions()


# --------------------------------------------------------------------------
# 事件流
# --------------------------------------------------------------------------
@app.get("/api/events")
def api_events() -> StreamingResponse:
    q: queue.Queue = queue.Queue(maxsize=500)

    def listener(evt: dict) -> None:
        try:
            q.put_nowait(evt)
        except queue.Full:
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
                    evt = q.get(timeout=15)
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


@app.get("/api/events/recent")
def api_events_recent(limit: int = 80) -> list[dict]:
    return list(reversed(memory.recent_events(max(0, min(limit, 1000)))))


# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------
@app.get("/api/knowledge")
def api_knowledge(
    topic: str | None = None, limit: int = 60, offset: int = 0, q: str | None = None
) -> dict:
    if q:
        items = memory.search(q, k=limit, topic=topic)
    else:
        items = memory.list_points(topic, limit, offset)
    topics = [{"topic": r[0]} for r in memory.conn().execute("SELECT DISTINCT topic FROM knowledge ORDER BY topic")]
    return {"items": items, "total": memory.count_points(topic), "topics": topics}


@app.delete("/api/knowledge/{pid}")
def api_knowledge_delete(pid: str) -> dict:
    memory.delete_point(pid)
    return {"ok": True}


# --------------------------------------------------------------------------
# 学习分析（增强可视化用）
# --------------------------------------------------------------------------
@app.get("/api/analytics")
def api_analytics() -> dict:
    from . import analytics

    return analytics.snapshot()


@app.get("/api/analytics/search")
def api_analytics_search(q: str = Query(...), k: int = 8) -> dict:
    from . import analytics

    return analytics.search_explain(q, k=min(max(1, int(k)), 30))


@app.get("/api/analytics/suggestions")
def api_analytics_suggestions() -> dict:
    """学习建议（引导）：下一步做什么，供总览「下一步建议」卡片使用。"""
    from . import analytics

    return {"items": analytics.suggestions()}


@app.get("/api/analytics/health")
def api_analytics_health() -> dict:
    """知识库健康度：总分 + 四个维度短板 + 最薄弱项，供总览健康度卡片使用。"""
    from . import analytics

    return analytics.health()


@app.get("/api/perf")
def api_perf() -> dict:
    """检索/聚合缓存的命中率与平均耗时，供分析视图的「检索性能」卡片展示。"""
    return memory.perf_stats()


@app.get("/api/knowledge/search")
def api_knowledge_search(q: str = Query(...), k: int = 8) -> list[dict]:
    return memory.search(q, k=k)


@app.get("/api/knowledge/cleanup")
def api_cleanup_state() -> dict:
    return cleanup.state()


class CleanupRequest(BaseModel):
    threshold: float = Field(.9, ge=.5, le=1.)
    token: str
    topic: str | None = None


@app.get("/api/knowledge/cleanup/preview")
def api_cleanup_preview(threshold: float = Query(.9, ge=.5, le=1.), topic: str | None = None) -> dict:
    return cleanup.preview(threshold, topic)


@app.post("/api/knowledge/cleanup")
def api_cleanup(body: CleanupRequest | None = None) -> dict:
    return cleanup.clean(**body.model_dump()) if body else cleanup.clean()


@app.post("/api/knowledge/cleanup/undo")
def api_cleanup_undo() -> dict:
    return cleanup.undo()


@app.get("/api/knowledge/duplicates")
def api_knowledge_duplicates(
    threshold: float = Query(.90, ge=.5, le=1.), limit: int = Query(30, ge=1, le=200), topic: str | None = None, offset: int = Query(0, ge=0)
) -> dict:
    """近重复知识点对（0.85~0.92 那一带：很像但没被去重挡掉的）。

    声明位置同样必须在 /api/knowledge/{pid} 之前，否则 "duplicates" 会被当 id。
    """
    return memory.near_duplicates(threshold=threshold, limit=limit, topic=topic, offset=offset)


@app.get("/api/knowledge/{pid}")
def api_knowledge_detail(pid: str, k: int = 5) -> dict:
    """单条知识点详情 + 语义最近邻，供知识库详情面板使用。

    必须声明在 /api/knowledge/search 之后：FastAPI 按声明顺序匹配，
    否则 "search" 会被当成 {pid} 吞掉。
    """
    item = memory.get_point(pid)
    if item is None:
        raise HTTPException(404, "知识点不存在")
    return {"item": item, "similar": memory.similar_points(pid, k=k)}


# --------------------------------------------------------------------------
# 资源
# --------------------------------------------------------------------------
@app.get("/api/resources")
def api_resources() -> dict:
    return {"items": memory.list_resources(), "disk": downloader.disk_report()}


@app.post("/api/resources/download")
def api_resource_download(body: dict = Body(...)) -> dict:
    repo = (body.get("repo") or "").strip()
    kind = body.get("kind") or "hf_model"
    if not repo:
        raise HTTPException(400, "缺少 repo")
    if kind == "ollama":
        return downloader.pull_ollama(repo, reason="手动添加")
    return downloader.download_hf(repo, kind=kind, reason="手动添加")


@app.post("/api/resources/discover")
def api_resource_discover(body: dict = Body(...)) -> dict:
    direction = (body.get("direction") or "").strip()
    if not direction:
        raise HTTPException(400, "缺少方向")
    got = downloader.discover_for_direction(direction)
    return {"ok": True, "found": got}


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


@app.get("/api/media/status")
def api_media_status() -> dict:
    """能力体检：视觉模型、语音识别、字幕、PDF、抽帧各自能不能用。"""
    caps = media.capabilities()
    caps["state"] = evolve.engine.snapshot()
    return caps


@app.post("/api/media/search")
def api_media_search(body: MediaSearchRequest) -> dict:
    kinds = [k for k in body.kinds if k in ("image", "audio", "video")]
    if not kinds:
        raise HTTPException(400, "kinds 只能是 image / audio / video")
    items = search.search_media(body.q, kinds=kinds, limit=body.limit)
    return {"items": items, "count": len(items)}


@app.post("/api/media/probe")
def api_media_probe(body: dict = Body(...)) -> dict:
    """不下载，只判断一个 URL 会被当成什么类型、能不能处理。

    无扩展名的地址（CDN / 图片代理）必须看响应头才能判准，
    所以这里对 http 链接真发一次 HEAD（失败退 1 字节 GET）。
    """
    url = str(body.get("url") or "").strip()
    if not url.startswith(("http://", "https://", "file://")):
        raise HTTPException(400, "URL 必须以 http/https/file 开头")
    info = media.probe(url) if url.startswith("http") else {"kind": media.kind_of(url)}
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


@app.post("/api/media/ingest")
def api_media_ingest(body: MediaIngestRequest) -> dict:
    res = evolve.engine.ingest_media(body.urls, body.direction or "")
    if not res.get("ok"):
        raise HTTPException(409, res.get("msg", "无法启动摄取"))
    return res


@app.post("/api/media/clear")
def api_media_clear() -> dict:
    return media.clear_cache()


@app.post("/api/media/backfill")
def api_media_backfill(body: MediaBackfillRequest | None = Body(default=None)) -> dict:
    """用图片/音频/视频给已有知识补漏（只找媒体能补的缺口）。"""
    b = body or MediaBackfillRequest()
    res = evolve.engine.backfill_media(
        b.topic or "", per_topic=b.per_topic, limit_topics=b.limit_topics
    )
    if not res.get("ok"):
        raise HTTPException(409, res.get("msg", "无法启动补漏"))
    return res


# --------------------------------------------------------------------------
# 对话（基于知识库）
# --------------------------------------------------------------------------
@app.post("/api/chat")
def api_chat(body: dict = Body(...)) -> dict:
    question = (body.get("question") or "").strip()
    images = body.get("images") or []
    if not question and not images:
        raise HTTPException(400, "缺少问题")
    return evolve.answer_with_knowledge(
        question or "请描述这些图片的内容", body.get("topic") or None, images=images
    )


@app.post("/api/chat/stream")
def api_chat_stream(body: dict = Body(...)) -> StreamingResponse:
    question = (body.get("question") or "").strip()
    topic = body.get("topic") or None
    images = body.get("images") or []
    if not question and not images:
        raise HTTPException(400, "缺少问题")

    def gen():
        try:
            res = evolve.answer_with_knowledge(question or "请描述这些图片的内容", topic, images=images)
            for i in range(0, len(res["answer"]), 24):
                yield f"data: {json.dumps({'delta': res['answer'][i:i+24]}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'sources': res['sources'], 'hits': res['hits']}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------
# 权重层
# --------------------------------------------------------------------------
@app.get("/api/finetune/env")
def api_ft_env() -> dict:
    return finetune.check_env()


@app.post("/api/finetune/install")
def api_ft_install() -> dict:
    def go() -> None:
        finetune.install_env(
            lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p})
        )

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True, "msg": "安装已在后台开始，约 3GB 下载"}


@app.post("/api/finetune/dataset")
def api_ft_dataset(body: dict = Body(default={})) -> dict:
    b = body or {}
    topic = (b.get("topic") or "").strip() or None
    return finetune.build_dataset_async(
        topic,
        min_samples=b.get("min_samples"),
        rewrite=bool(b.get("rewrite", True)),
        on_event=lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p}),
    )


@app.post("/api/finetune/train")
def api_ft_train(body: dict = Body(...)) -> dict:
    path = body.get("dataset_path")
    if not path:
        raise HTTPException(400, "缺少 dataset_path")
    return finetune.train_async(
        path,
        on_event=lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p}),
        base=body.get("base"),
        epochs=body.get("epochs"),
    )


@app.get("/api/finetune/state")
def api_ft_state() -> dict:
    return finetune.state()


@app.get("/api/finetune/outputs")
def api_ft_outputs() -> list[dict]:
    return finetune.list_outputs()


@app.post("/api/finetune/export")
def api_ft_export(body: dict = Body(...)) -> dict:
    return finetune.export_gguf(
        body.get("merged_dir", ""),
        on_event=lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p}),
    )


# --------------------------------------------------------------------------
# 数据备份 / 恢复
# --------------------------------------------------------------------------
@app.get("/api/backups")
def api_backups() -> dict:
    return {"items": backup.list_backups(), "keep": backup.KEEP_LATEST}


@app.post("/api/backups/export")
def api_backup_export(body: dict = Body(default={})) -> dict:
    label = ((body or {}).get("label") or "").strip()
    try:
        return backup.export_backup(label)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"备份失败：{e}") from e


@app.get("/api/backups/download/{name}")
def api_backup_download(name: str) -> FileResponse:
    try:
        p = backup.backup_path(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not p.exists():
        raise HTTPException(404, "备份不存在")
    return FileResponse(p, filename=name, media_type="application/zip")


@app.delete("/api/backups/{name}")
def api_backup_delete(name: str) -> dict:
    try:
        backup.delete_backup(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@app.post("/api/backups/restore")
async def api_backup_restore(
    file: UploadFile = File(...), confirm: bool = Body(False)
) -> dict:
    # 先落临时文件再走校验，不把几十上百 MB 的 zip 读进内存
    staging = Path(tempfile.mkdtemp(prefix="evomind-restore-upload-",
                                   dir=str(config.DATA)))
    try:
        src = staging / "upload.zip"
        with open(src, "wb") as f:
            shutil.copyfileobj(file.file, f, length=1024 * 1024)
        if not src.stat().st_size:
            raise HTTPException(400, "上传的文件是空的")
        try:
            return backup.restore_backup(src, confirm=confirm)
        except FileNotFoundError as e:
            raise HTTPException(400, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------
@app.get("/api/models")
def api_models() -> list[dict]:
    return llm.list_models()


@app.post("/api/models/use")
def api_models_use(body: dict = Body(...)) -> dict:
    name = body.get("name")
    if not name:
        raise HTTPException(400, "缺少模型名")
    return config.update({"model": name})


# --------------------------------------------------------------------------
# 本地书库
# --------------------------------------------------------------------------
def _lib_evt(lv: str, msg: str, payload: dict) -> None:
    evolve.bus.publish({"level": lv, "msg": msg, **payload})


@app.get("/api/library/stats")
def api_lib_stats() -> dict:
    return {"stats": library.library_stats(), "task": library.state()}


@app.get("/api/library/dirs")
def api_lib_dirs() -> list[dict]:
    return library.list_dirs()


@app.post("/api/library/dirs")
def api_lib_add_dir(body: dict = Body(...)) -> dict:
    path = (body.get("path") or "").strip().strip('"')
    if not path:
        raise HTTPException(400, "缺少目录路径")
    return library.add_dir(
        path,
        body.get("label") or "",
        body.get("excludes") if isinstance(body.get("excludes"), list) else None,
    )


@app.patch("/api/library/dirs/{did}")
def api_lib_update_dir(did: str, body: dict = Body(...)) -> dict:
    return library.update_dir(did, **body)


@app.delete("/api/library/dirs/{did}")
def api_lib_del_dir(did: str, purge: bool = False) -> dict:
    return library.remove_dir(did, purge)


@app.post("/api/library/scan")
def api_lib_scan(body: dict = Body(default={})) -> dict:
    dirs = (body or {}).get("dirs") or None
    return library.scan_async(dirs, on_event=_lib_evt)


@app.get("/api/library/books")
def api_lib_books(
    status: str | None = None,
    dir_id: str | None = None,
    fmt: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    return library.list_books(
        status=status, dir_id=dir_id, fmt=fmt, q=q, limit=limit, offset=offset
    )


@app.get("/api/library/books/{bid}")
def api_lib_book(bid: str) -> dict:
    d = library.book_detail(bid)
    if not d:
        raise HTTPException(404, "书目不存在")
    return d


@app.post("/api/library/books/{bid}/parse")
@tasks.exclusive("书目解析")
def api_lib_parse(bid: str) -> dict:
    return library.parse_book(bid, on_event=_lib_evt)


@app.post("/api/library/import")
def api_lib_import(body: dict = Body(...)) -> dict:
    ids = body.get("book_ids") or []
    if not ids:
        raise HTTPException(400, "请选择要导入的书")
    return library.import_books_async(
        ids,
        topic=(body.get("topic") or "").strip(),
        max_chunks=int(body.get("max_chunks") or library.MAX_CHUNKS_PER_BOOK),
        on_event=_lib_evt,
    )


@app.post("/api/library/parse")
def api_lib_parse(body: dict = Body(...)) -> dict:
    """批量解析：只提取正文、标出扫描版，不做 LLM 提炼。"""
    ids = body.get("book_ids") or []
    if not ids:
        raise HTTPException(400, "请选择要解析的书")
    return library.parse_books_async(ids, on_event=_lib_evt)


@app.post("/api/library/import-url")
def api_lib_import_url(body: dict = Body(...)) -> dict:
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少链接")
    return library.import_url(url, (body.get("topic") or "").strip(), on_event=_lib_evt)


@app.get("/api/library/state")
def api_lib_state() -> dict:
    return library.state()


@app.post("/api/library/stop")
def api_lib_stop() -> dict:
    return library.stop_task()


@app.delete("/api/library/books/{bid}")
def api_lib_del_book(bid: str, purge_points: bool = False) -> dict:
    return library.delete_book(bid, purge_points)


# --------------------------------------------------------------------------
# 扫描件 OCR
# --------------------------------------------------------------------------
@app.get("/api/ocr/state")
def api_ocr_state() -> dict:
    return ocr.state()


@app.post("/api/ocr/stop")
def api_ocr_stop() -> dict:
    return ocr.stop()


@app.get("/api/ocr/vision")
def api_ocr_vision() -> dict:
    ok, info = ocr.vision_available()
    return {"available": ok, "model": info}


@app.get("/api/ocr/device")
def api_ocr_device() -> dict:
    """识别后端状态：GPU 是否真的在用、并发几线程。"""
    return ocr.device_status()


@app.post("/api/library/books/{bid}/ocr")
def api_book_ocr(bid: str, body: dict = Body(...)) -> dict:
    return ocr.ocr_book_async(
        bid,
        pages=str(body.get("pages") or "all"),
        engine=body.get("engine") or "rapid",
        dpi=int(body.get("dpi") or config.get("ocr_dpi", 200)),
        skip_done=bool(body.get("skip_done", True)),
        on_event=_lib_evt,
    )


@app.post("/api/ocr/batch")
def api_ocr_batch(body: dict = Body(...)) -> dict:
    """批量识别多本书：一本跑完再开下一本。"""
    return ocr.ocr_batch_async(
        body.get("book_ids") or [],
        pages=str(body.get("pages") or "all"),
        engine=body.get("engine") or "rapid",
        dpi=int(body.get("dpi") or config.get("ocr_dpi", 200)),
        skip_done=bool(body.get("skip_done", True)),
        on_event=_lib_evt,
    )


@app.get("/api/library/books/{bid}/ocr")
def api_book_ocr_pages(bid: str) -> dict:
    return {"pages": ocr.list_pages(bid), "stats": ocr.ocr_stats(bid)}


@app.get("/api/library/books/{bid}/ocr/{page_no}")
def api_book_ocr_page(bid: str, page_no: int) -> dict:
    d = ocr.get_page(bid, page_no)
    if not d:
        raise HTTPException(404, "这一页还没有识别结果")
    return d


@app.put("/api/library/books/{bid}/ocr/{page_no}")
def api_book_ocr_save(bid: str, page_no: int, body: dict = Body(...)) -> dict:
    return ocr.save_page(bid, page_no, body.get("text") or "")


@app.post("/api/library/books/{bid}/ocr/{page_no}/redo")
def api_book_ocr_redo(bid: str, page_no: int, body: dict = Body(default={})) -> dict:
    return ocr.ocr_page(
        bid, page_no,
        engine=(body or {}).get("engine") or "rapid",
        dpi=int((body or {}).get("dpi") or config.get("ocr_dpi", 200)),
    )


@app.get("/api/library/books/{bid}/page/{page_no}.png")
def api_book_page_png(bid: str, page_no: int, dpi: int = 110) -> Response:
    data = ocr.render_png(bid, page_no, dpi)
    if not data:
        raise HTTPException(404, "页面渲染失败")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/ocr/image")
async def api_ocr_image(file: UploadFile = File(...), engine: str = "rapid") -> dict:
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(400, "图片超过 30MB")
    return ocr.ocr_image_bytes(data, engine)


@app.on_event("startup")
def _startup() -> None:
    memory.log_event("boot", "EvoMind 启动")
    try:
        library.init_db()
        # 上次退出时被中断的导入会留下僵尸状态，启动时还原
        library.recover_stale_states()
    except Exception:
        pass
    try:
        curriculum.recover_stale()
    except Exception:
        pass
    try:
        scheduler.start_auto_if_needed()
    except Exception:
        pass

    # 后台探测 Ollama；论坛索引按需加载，避免启动就占用网络和索引锁。
    # Ollama 未启动时健康探测要等满连接超时（实测 2 秒），放在 startup 里
    # 会让 uvicorn 迟迟不开始 accept——第一屏要等它，等于开机就卡 2 秒。
    def _warm() -> None:
        # 后台线程里可以放心同步探测：这里阻塞不影响任何请求
        try:
            if not llm.tags(fresh=True)[0]:
                memory.log_event("warn", "Ollama 未运行")
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True).start()


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
