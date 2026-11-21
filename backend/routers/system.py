"""系统：运行状态、配置读写、本地模型切换。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Body, HTTPException

from .. import config, curriculum, downloader, evolve, finetune, llm, memory, scheduler, refinement
from ._common import APP_REVISION

router = APIRouter()


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
@router.get("/api/status")
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
        "config": config.public_view(),
        "disk": downloader.disk_report(),
        "finetune": {"ready": env["ready"], "gpu": env.get("gpu"), "cuda": env.get("cuda")},
        "finetune_state": finetune.state(),
        # 本次状态接口自身的端到端耗时（ms）。前端据此画一个延迟徽标，
        # 也正好是「优化有没有让轮询变快」的最直观证据（命中缓存后这项会明显降）。
        "response_ms": round((time.perf_counter() - t0) * 1000, 2),
    }
    memory.record_status_poll((time.perf_counter() - t0) * 1000)
    return out


@router.get("/api/config")
def api_config_get() -> dict:
    return config.public_view()


@router.post("/api/config")
def api_config_set(patch: dict = Body(...)) -> dict:
    allowed = set(config.DEFAULTS.keys())
    # 前端拿到的是掩码；原样回帖必须视作「不修改」，否则用户保存设置时
    # 会把真实密钥覆盖成掩码字符串。
    clean = {k: v for k, v in patch.items()
             if k in allowed and not (k in config.SECRET_KEYS and v == config.SECRET_MASK)}
    try:
        return config.update(clean)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------
@router.get("/api/models")
def api_models() -> list[dict]:
    return llm.list_models()


@router.post("/api/models/use")
def api_models_use(body: dict = Body(...)) -> dict:
    name = body.get("name")
    if not name:
        raise HTTPException(400, "缺少模型名")
    return config.update({"model": name})
