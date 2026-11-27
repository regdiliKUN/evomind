"""EvoMind HTTP 服务：应用装配（中间件、异常处理、静态资源、生命周期）。

业务接口按域拆在 ``backend/routers/`` 下（system / learning / events / knowledge /
analytics / topics / resources / media / chat / finetune / backups / library / ocr /
review），这里只负责把它们按固定顺序挂到 app 上。
"""
from __future__ import annotations

import threading

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, curriculum, library, llm, memory, scheduler
from .routers import ROUTERS
from .routers._common import APP_REVISION  # noqa: F401  兼容旧引用 main.APP_REVISION

app = FastAPI(title="EvoMind", version="1.0.0")

WEB = config.WEB
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

# --- 安全基础设施 -------------------------------------------------------
#: 只接受这些 Host。服务虽绑在 127.0.0.1，公网页面可借 DNS rebinding 把
#: 域名指回本机；Host 校验让这类请求在路由之前就失效。testserver 是
#: FastAPI TestClient 的默认 Host，保留它以免破坏离线回归测试。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})


@app.middleware("http")
async def _local_guard(request: Request, call_next):
    """本机 Host + 同源 Origin 守卫，挡 DNS rebinding 与跨站表单写入。"""
    from urllib.parse import urlsplit

    raw_host = request.headers.get("host") or ""
    try:
        host = (urlsplit("//" + raw_host).hostname or "").lower()
    except ValueError:
        # 畸形 IPv6（如 "[::1"）会让 urlsplit 抛 ValueError，
        # 未捕获就变成 500；守卫自身不该成为异常来源。
        return JSONResponse(status_code=400, content={"error": "Host 头格式不合法"})
    if "@" in raw_host or host not in _LOCAL_HOSTS:
        return JSONResponse(status_code=403, content={"error": "仅允许本机访问"})
    origin = request.headers.get("origin")
    if origin:
        try:
            o = urlsplit(origin)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "Origin 头格式不合法"})
        if (o.scheme not in ("http", "https") or "@" in (o.netloc or "")
                or (o.hostname or "").lower() not in _LOCAL_HOSTS):
            return JSONResponse(status_code=403, content={"error": "拒绝跨源请求"})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path == "/" or request.url.path.startswith("/static/"):
        # 首页与前端资源必须每次向本机服务确认（no-cache = 可存但每次用 ETag 复核）。
        # 之前只给 /api/ 设了缓存头：Edge 应用窗口按启发式新鲜度直接复用旧 index.html，
        # 旧页面又引用旧 ?v= 的 app.js —— 升级后重启多少次都看不到新功能。
        response.headers.setdefault("Cache-Control", "no-cache")
    # /docs、/redoc 用 CDN 脚本 + 内联脚本，单独放行，不套 CSP。
    if not request.url.path.startswith(("/docs", "/redoc")):
        response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


#: 前端只有同源 app.js，无内联脚本（媒体缩略图的 onerror 已改事件委托），
#: 所以 script-src 不放 'unsafe-inline'；内联 style= 仍有约 190 处，style-src 需放行。
#: 媒体检索缩略图来自外部站点，img-src 放行 http(s)；请求与 SSE 均同源。
_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: http: https:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))


@app.exception_handler(Exception)
def _unhandled(request, exc: Exception) -> JSONResponse:
    """把后端异常转成前端能读的 JSON，而不是一片空白。

    严苛评审修订：原实现把 str(exc) 与 1200 字符 traceback 原样回传浏览器，
    会泄露本地绝对路径、SQL 片段、配置值与模型输出。现在只回一个短错误 id，
    完整堆栈留在服务端事件台账，凭 id 可查。
    """
    import traceback
    import uuid

    err_id = uuid.uuid4().hex[:12]
    memory.log_event("error", f"接口异常[{err_id}] {request.url.path}: {exc}",
                     {"trace": traceback.format_exc()[-1200:]})
    return JSONResponse(
        status_code=500,
        content={"error": "服务内部错误，请查看事件台账", "error_id": err_id,
                 "path": str(request.url.path)},
    )


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


# --------------------------------------------------------------------------
# 业务接口：按 routers.ROUTERS 的顺序注册（顺序即匹配优先级）
# --------------------------------------------------------------------------
for _router in ROUTERS:
    app.include_router(_router)
del _router

# 兼容导出：测试、基准脚本（scripts/bench/*、tests/bench_*）以 main.api_xxx 直接调用
# 接口函数，拆分后仍从这里可达。注意：patch main.xxx 不会影响路由——要打桩请
# patch 路由所在模块（backend.routers.<域>）或其依赖的业务模块。
from .routers._common import _flag, _ids, _lib_evt, _looks_like_image, _num, _safe_under, _text  # noqa: E402,F401
from .routers.analytics import (  # noqa: E402,F401
    api_analytics,
    api_analytics_acquisition,
    api_analytics_health,
    api_analytics_quality,
    api_analytics_search,
    api_analytics_suggestions,
    api_evidence_policy,
    api_perf,
)
from .routers.backups import (  # noqa: E402,F401
    MAX_RESTORE_BYTES,
    api_backup_delete,
    api_backup_download,
    api_backup_export,
    api_backup_restore,
    api_backups,
)
from .routers.chat import api_chat, api_chat_stream  # noqa: E402,F401
from .routers.digest import (  # noqa: E402,F401
    api_digest_delete,
    api_digest_get,
    api_digest_markdown,
    api_digest_stream,
    api_digests,
)
from .routers.events import (  # noqa: E402,F401
    api_events,
    api_events_alerts,
    api_events_archive,
    api_events_recent,
    api_events_rollups,
)
from .routers.finetune import (  # noqa: E402,F401
    api_ft_dataset,
    api_ft_env,
    api_ft_export,
    api_ft_install,
    api_ft_outputs,
    api_ft_state,
    api_ft_train,
)
from .routers.knowledge import (  # noqa: E402,F401
    CleanupRequest,
    FeedbackRequest,
    KnowledgePatch,
    SourceCheckRequest,
    api_cleanup,
    api_cleanup_preview,
    api_cleanup_state,
    api_cleanup_undo,
    api_knowledge,
    api_knowledge_delete,
    api_knowledge_detail,
    api_knowledge_duplicates,
    api_knowledge_feedback,
    api_knowledge_feedback_list,
    api_knowledge_hybrid_search,
    api_knowledge_patch,
    api_knowledge_revisions,
    api_knowledge_search,
    api_knowledge_verify,
    api_search_index_rebuild,
    api_search_index_status,
    api_source_check,
)
from .routers.learning import (  # noqa: E402,F401
    AutoRequest,
    DirectionRequest,
    LearnRequest,
    RefineRequest,
    RotationPlanRequest,
    RotationRequest,
    api_auto_get,
    api_auto_set,
    api_turbo_get,
    api_turbo_set,
    api_directions,
    api_learn_start,
    api_learn_stop,
    api_refine_books,
    api_refine_start,
    api_refine_stop,
    api_refinements,
    api_rotation,
    api_rotation_pause,
    api_rotation_plan,
    api_rotation_resume,
    api_rotation_skip,
    api_rotation_start,
    api_rotation_stop,
    api_rotation_summary,
    api_sessions,
)
from .routers.library import (  # noqa: E402,F401
    api_lib_add_dir,
    api_lib_book,
    api_lib_books,
    api_lib_del_book,
    api_lib_del_dir,
    api_lib_dirs,
    api_lib_import,
    api_lib_import_url,
    api_lib_parse,
    api_lib_parse_one,
    api_lib_scan,
    api_lib_state,
    api_lib_stats,
    api_lib_stop,
    api_lib_update_dir,
)
from .routers.media import (  # noqa: E402,F401
    MediaBackfillRequest,
    MediaIngestRequest,
    MediaSearchRequest,
    api_media_backfill,
    api_media_clear,
    api_media_ingest,
    api_media_probe,
    api_media_search,
    api_media_status,
)
from .routers.ocr import (  # noqa: E402,F401
    MAX_OCR_IMAGE_BYTES,
    api_book_ocr,
    api_book_ocr_page,
    api_book_ocr_pages,
    api_book_ocr_redo,
    api_book_ocr_save,
    api_book_page_png,
    api_ocr_batch,
    api_ocr_device,
    api_ocr_image,
    api_ocr_state,
    api_ocr_stop,
    api_ocr_vision,
)
from .routers.resources import api_resource_discover, api_resource_download, api_resources  # noqa: E402,F401
from .routers.review import (  # noqa: E402,F401
    api_review_enqueue,
    api_review_plan,
    api_review_run_local,
    api_review_run_urls,
    api_review_status,
)
from .routers.system import (  # noqa: E402,F401
    api_config_get,
    api_config_set,
    api_models,
    api_models_use,
    api_status,
)
from .routers.topics import (  # noqa: E402,F401
    api_topics_add_alias,
    api_topics_add_term,
    api_topics_clusters,
    api_topics_del_alias,
    api_topics_del_term,
    api_topics_reclassify,
    api_topics_vocabulary,
)


def _startup_warn(label: str, exc: Exception) -> None:
    """启动期子步骤失败必须留痕；写台账失败时退到 stderr，绝不静默。"""
    import sys

    try:
        memory.log_event("warn", f"{label}失败：{exc}")
    except Exception:  # noqa: BLE001  台账本身可能也坏了
        print(f"[EvoMind] {label}失败：{exc}", file=sys.stderr)


@app.on_event("startup")
def _startup() -> None:
    memory.log_event("boot", "EvoMind 启动")
    try:
        library.init_db()
        # 上次退出时被中断的导入会留下僵尸状态，启动时还原
        library.recover_stale_states()
    except Exception as e:  # noqa: BLE001
        _startup_warn("书库初始化", e)
    try:
        curriculum.recover_stale()
    except Exception as e:  # noqa: BLE001
        _startup_warn("轮换状态恢复", e)
    try:
        memory.close_stale_sessions()
    except Exception as e:  # noqa: BLE001
        _startup_warn("学习会话状态恢复", e)
    try:
        from . import turbo

        turbo.apply()  # 上次关机前开着狂暴模式：重启后防睡眠 / 优先级要重新生效
    except Exception as e:  # noqa: BLE001
        _startup_warn("狂暴模式", e)
    try:
        scheduler.start_auto_if_needed()
    except Exception as e:  # noqa: BLE001
        _startup_warn("持续自学启动", e)

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
