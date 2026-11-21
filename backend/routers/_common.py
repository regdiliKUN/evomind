"""路由模块共享的请求体收敛 / 安全校验工具与应用常量。"""
from __future__ import annotations

import math
from pathlib import Path

from fastapi import HTTPException

from .. import evolve

APP_REVISION = "20261023.1"


def _looks_like_image(data: bytes) -> bool:
    """按魔数做最低限度校验，挡掉把任意文件塞进 OCR 通道的尝试。"""
    magic = (
        bytes.fromhex("89504e470d0a1a0a"),  # PNG
        bytes.fromhex("ffd8ff"),             # JPEG
        b"GIF87a", b"GIF89a", b"BM", b"%PDF-",
        bytes.fromhex("49492a00"),           # TIFF LE
        bytes.fromhex("4d4d002a"),           # TIFF BE
    )
    if any(data.startswith(m) for m in magic):
        return True
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _safe_under(raw: str, base: Path) -> Path:
    """把请求体里的路径收敛到 base 目录内，拒绝绝对路径与 .. 逃逸。"""
    try:
        p = Path(raw).resolve()
        b = base.resolve()
        if p != b and b not in p.parents:
            raise ValueError
    except (OSError, ValueError) as e:
        raise HTTPException(400, "路径不在允许的目录内") from e
    return p


def _num(raw, default, low, high, cast=int):
    """把请求体里的任意值收敛到 [low, high]；非法值回退默认，不抛 500。"""
    if isinstance(raw, bool):
        return default
    try:
        v = cast(raw)
        if not math.isfinite(v):
            return default
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(v, low), high)


def _text(raw, default="", limit=200):
    """字符串收敛：去空白 + 限长；None 与容器类型回退默认。"""
    if raw is None or isinstance(raw, (dict, list)):
        return default
    return str(raw).strip()[:limit]


def _ids(raw, cap=200):
    """ID 列表收敛：限定条数与单条长度，挡住超大数组打满内存。"""
    if not isinstance(raw, list):
        return []
    return [str(x)[:128] for x in raw[:cap] if x is not None and x != ""]


def _flag(raw, default=True):
    """布尔解析：bool 原样；字符串 'false/0/no/off/空' 才视为假。

    原实现到处用 bool(body.get(...))，前端若传字符串 "false" 会得到 True，
    于是「本该只报数」的 dry_run 变成真的动库。
    """
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("false", "0", "no", "off", "")


def _lib_evt(lv: str, msg: str, payload: dict) -> None:
    """后台任务（书库 / OCR）的进度回调：转发到 SSE 事件总线。"""
    evolve.bus.publish({"level": lv, "msg": msg, **payload})
