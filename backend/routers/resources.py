"""资源层：HF 模型/数据集与 Ollama 模型的发现与下载。"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from .. import downloader, memory

router = APIRouter()


# --------------------------------------------------------------------------
# 资源
# --------------------------------------------------------------------------
@router.get("/api/resources")
def api_resources() -> dict:
    return {"items": memory.list_resources(), "disk": downloader.disk_report()}


@router.post("/api/resources/download")
def api_resource_download(body: dict = Body(...)) -> dict:
    repo = (body.get("repo") or "").strip()
    kind = body.get("kind") or "hf_model"
    if not repo:
        raise HTTPException(400, "缺少 repo")
    if kind == "ollama":
        return downloader.pull_ollama(repo, reason="手动添加")
    return downloader.download_hf(repo, kind=kind, reason="手动添加")


@router.post("/api/resources/discover")
def api_resource_discover(body: dict = Body(...)) -> dict:
    direction = (body.get("direction") or "").strip()
    if not direction:
        raise HTTPException(400, "缺少方向")
    got = downloader.discover_for_direction(direction)
    return {"ok": True, "found": got}
