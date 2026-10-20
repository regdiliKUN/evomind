"""EvoMind HTTP 服务：REST API + SSE 实时事件流 + 静态控制台。"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, downloader, evolve, finetune, library, llm, memory, ocr, scheduler

app = FastAPI(title="EvoMind", version="1.0.0")

WEB = config.WEB
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


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
        "engine": evolve.engine.snapshot(),
        "stats": memory.stats(),
        "llm": {"healthy": ok, "models": models},
        "config": config.load(),
        "disk": downloader.disk_report(),
        "finetune": {"ready": env["ready"], "gpu": env.get("gpu"), "cuda": env.get("cuda")},
        "finetune_state": finetune.state(),
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
def api_learn_start(body: dict = Body(...)) -> dict:
    direction = (body.get("direction") or "").strip()
    rounds = body.get("rounds")
    if not direction:
        raise HTTPException(400, "请填写学习方向")
    config.update({"last_direction": direction})
    res = evolve.engine.start(direction, rounds)
    if res.get("ok"):
        memory.log_event("start", f"启动学习：{direction}", {"rounds": rounds})
    return res


@app.post("/api/learn/stop")
def api_learn_stop() -> dict:
    return evolve.engine.stop()


# --------------------------------------------------------------------------
# 持续自学
# --------------------------------------------------------------------------
@app.get("/api/auto")
def api_auto_get() -> dict:
    return scheduler.state()


@app.post("/api/auto")
def api_auto_set(body: dict = Body(...)) -> dict:
    on = bool(body.get("enabled"))
    direction = (body.get("direction") or "").strip() or None
    if body.get("idle_minutes") is not None:
        config.update({"idle_minutes": max(0, int(body["idle_minutes"]))})
    if body.get("auto_rounds") is not None:
        config.update({"auto_rounds": max(1, int(body["auto_rounds"]))})
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
    return {"items": items, "total": memory.count_points(topic)}


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
# 对话（基于知识库）
# --------------------------------------------------------------------------
@app.post("/api/chat")
def api_chat(body: dict = Body(...)) -> dict:
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "缺少问题")
    return evolve.answer_with_knowledge(question, body.get("topic") or None)


@app.post("/api/chat/stream")
def api_chat_stream(body: dict = Body(...)) -> StreamingResponse:
    question = (body.get("question") or "").strip()
    topic = body.get("topic") or None
    if not question:
        raise HTTPException(400, "缺少问题")

    def gen():
        try:
            res = evolve.answer_with_knowledge(question, topic)
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
    return library.scan(dirs, on_event=_lib_evt)


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
    if not llm.healthy():
        memory.log_event("warn", "Ollama 未运行")
    try:
        scheduler.start_auto_if_needed()
    except Exception:
        pass

    # 后台预热 ZBrushCentral 的主题索引（首次要拉十几页，别让用户等）
    def _warm() -> None:
        try:
            from . import search as _s

            n = len(_s._load_zbc_topics())
            memory.log_event("info", f"ZBrushCentral 索引就绪：{n} 个主题")
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True).start()


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
