"""权重层：环境检测/安装、数据集构建、LoRA 训练、产物列表与 GGUF 导出。"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Body, HTTPException

from .. import config, evolve, finetune
from ._common import _safe_under, _num, _text, _flag

router = APIRouter()


# --------------------------------------------------------------------------
# 权重层
# --------------------------------------------------------------------------
@router.get("/api/finetune/env")
def api_ft_env() -> dict:
    return finetune.check_env()


@router.post("/api/finetune/install")
def api_ft_install() -> dict:
    def go() -> None:
        finetune.install_env(
            lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p})
        )

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True, "msg": "安装已在后台开始，约 3GB 下载"}


@router.post("/api/finetune/dataset")
def api_ft_dataset(body: dict = Body(default={})) -> dict:
    b = body or {}
    topic = _text(b.get("topic"), limit=200) or None
    ms = b.get("min_samples")
    return finetune.build_dataset_async(
        topic,
        min_samples=_num(ms, 300, 1, 1000000) if ms is not None else None,
        rewrite=_flag(b.get("rewrite", True)),
        on_event=lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p}),
    )


@router.post("/api/finetune/train")
def api_ft_train(body: dict = Body(...)) -> dict:
    path = body.get("dataset_path")
    if not path:
        raise HTTPException(400, "缺少 dataset_path")
    # dataset_path 来自请求体，原样传给 open() 等于开放任意文件读取。
    # 收敛到允许的目录内，且必须是已存在的真实文件。
    # 「构建数据集」实际写到 finetune.OUTPUTS（data/finetune/dataset_*.jsonl），
    # 原先只放行 data/datasets，导致界面上「开始微调」恒报「路径不在允许的目录内」。
    safe = None
    for base in (config.DATASETS, finetune.OUTPUTS):
        try:
            safe = _safe_under(str(path), base)
            break
        except HTTPException:
            continue
    if safe is None:
        raise HTTPException(400, "路径不在允许的目录内")
    if not safe.is_file():
        raise HTTPException(400, "数据集文件不存在")
    return finetune.train_async(
        str(safe),
        on_event=lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p}),
        base=_text(body.get("base"), limit=200) or None,
        epochs=(_num(body.get("epochs"), config.get("finetune_epochs", 2), 1, 100)
                if body.get("epochs") is not None else None),
        lr=(_num(body.get("lr"), config.get("finetune_lr", 1e-4), 1e-10, 1.0, float)
            if body.get("lr") is not None else None),
    )


@router.get("/api/finetune/state")
def api_ft_state() -> dict:
    return finetune.state()


@router.get("/api/finetune/outputs")
def api_ft_outputs() -> list[dict]:
    return finetune.list_outputs()


@router.post("/api/finetune/export")
def api_ft_export(body: dict = Body(...)) -> dict:
    raw = str(body.get("merged_dir") or "")
    if not raw:
        raise HTTPException(400, "缺少 merged_dir")
    safe = _safe_under(raw, config.DATA / "finetune")
    if not safe.is_dir():
        raise HTTPException(400, "合并模型目录不存在")
    return finetune.export_gguf(
        str(safe),
        on_event=lambda lv, m, p: evolve.bus.publish({"level": lv, "msg": m, **p}),
    )
