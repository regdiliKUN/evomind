"""数据备份 / 恢复。"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import backup, config

router = APIRouter()


MAX_RESTORE_BYTES = 512 * 1024 * 1024


# --------------------------------------------------------------------------
# 数据备份 / 恢复
# --------------------------------------------------------------------------
@router.get("/api/backups")
def api_backups() -> dict:
    return {"items": backup.list_backups(), "keep": backup.KEEP_LATEST}


@router.post("/api/backups/export")
def api_backup_export(body: dict = Body(default={})) -> dict:
    label = ((body or {}).get("label") or "").strip()
    try:
        return backup.export_backup(label)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"备份失败：{e}") from e


@router.get("/api/backups/download/{name}")
def api_backup_download(name: str) -> FileResponse:
    try:
        p = backup.backup_path(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not p.exists():
        raise HTTPException(404, "备份不存在")
    return FileResponse(p, filename=name, media_type="application/zip")


@router.delete("/api/backups/{name}")
def api_backup_delete(name: str) -> dict:
    try:
        backup.delete_backup(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@router.post("/api/backups/restore")
def api_backup_restore(
    file: UploadFile = File(...), confirm: bool = Body(False)
) -> dict:
    # 先落临时文件再走校验，不把几十上百 MB 的 zip 读进内存
    staging = Path(tempfile.mkdtemp(prefix="evomind-restore-upload-",
                                   dir=str(config.DATA)))
    try:
        src = staging / "upload.zip"
        # 流式落盘并计数：Content-Length 可以伪造，分块上传也能绕过，
        # 所以必须在真正写入时累计字节数，超上限立刻拒掉。
        with open(src, "wb") as f:
            written = 0
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_RESTORE_BYTES:
                    raise HTTPException(413, "备份文件超过 512MB")
                f.write(chunk)
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
