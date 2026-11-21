"""EvoMind 学习数据备份 / 恢复。

一键备份把 SQLite 主库打成自包含 zip：

    knowledge.db   在线一致性快照（引擎正在写也能导，见 _snapshot_db）
    config.json    当前配置（密钥类字段打码）
    manifest.json  备份时间、各表行数、来源信息
    README.txt     手动恢复步骤（新机器 / 新实例兜底）

恢复走「先校验、再自动兜底备份、最后原子替换」，全程可回退；
引擎 / 书库 / OCR / 持续自学有任务在跑时直接拒绝，避免把正在写入的库换掉。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

DB_MEMBER = "knowledge.db"
CONFIG_MEMBER = "config.json"
MANIFEST_MEMBER = "manifest.json"
README_MEMBER = "README.txt"

#: 文件名白名单：evomind-backup-YYYYMMDD-HHMMSS[-slug].zip（slug 可含连字符）。
#: 下载 / 删除只认这个形状，挡住 ../ 之类的花样。
NAME_RE = re.compile(r"^evomind-backup-\d{8}-\d{6}(?:-[a-z0-9][a-z0-9-]{0,23})?\.zip$")

#: 备份保留上限。超出删最旧的（名字带时间戳，字典序即时间序），
#: 防止每次点一下都存一份把磁盘塞满。
KEEP_LATEST = 10

#: 打包时打码的字段：属于用户隐私，随 zip 分享会泄露，恢复后需在设置里重填。
REDACT_KEYS = ("search_api_key", "proxy")

#: 备份 zip 里单独列行数的表。
COUNTED_TABLES = (
    "knowledge", "documents", "sessions", "resources", "events",
    "library_dirs", "books", "book_chapters", "ocr_pages",
)

MANIFEST_VERSION = 1


# --------------------------------------------------------------------------
# 路径与命名
# --------------------------------------------------------------------------
def _dir() -> Path:
    """备份目录。每次调用现取 config.DATA：测试会替换配置路径。"""
    return config.DATA / "backups"


def backup_path(name: str) -> Path:
    """把用户传来的文件名收敛成 data/backups 下的一个确定路径。"""
    if not NAME_RE.match(name or ""):
        raise ValueError(f"非法备份文件名：{name!r}")
    base = _dir()
    p = (base / name).resolve()
    if p.parent != base.resolve():
        raise ValueError(f"非法备份文件名：{name!r}")
    return p


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(label or "").lower()).strip("-")
    return s[:24]


def _human(size: int) -> str:
    v = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{size} B"


# --------------------------------------------------------------------------
# 库检测
# --------------------------------------------------------------------------
def _counts(db: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        c = sqlite3.connect(str(db))
    except sqlite3.Error:
        return out
    try:
        for t in COUNTED_TABLES:
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = None
    finally:
        c.close()
    return out


def _quick_check(db: Path) -> tuple[bool, str]:
    """SQLite 官方完整性快检：坏页 / 截断都会在这里现形。"""
    try:
        c = sqlite3.connect(str(db))
        try:
            row = c.execute("PRAGMA quick_check").fetchone()
        finally:
            c.close()
    except sqlite3.Error as e:
        return False, str(e)
    msg = str(row[0]) if row else ""
    return msg.lower() == "ok", msg


def _snapshot_db(dest: Path) -> dict[str, Any]:
    """把当前主库做成自包含单文件快照，已提交的 WAL 内容一并带上。

    VACUUM INTO 是 SQLite 官方的在线快照入口：对源库只加读锁、逐页拷贝，
    产出不带 -wal/-shm 的紧凑单文件。老版本 sqlite 没有这条语句，
    退回 backup API（效果等价，产物体积略大）。
    直接复制 .db 文件在 WAL 模式下可能拷到漏页的半成品，故不用。
    """
    src = sqlite3.connect(str(config.DB_FILE), timeout=15.0)
    try:
        src.execute("PRAGMA busy_timeout=15000")
        try:
            src.execute("VACUUM INTO ?", (str(dest),))
            return _counts(dest)
        except sqlite3.Error:
            pass  # 老版本 sqlite：走 backup API
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            try:
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.commit()
            except sqlite3.Error:
                pass
        finally:
            dst.close()
    finally:
        src.close()
    return _counts(dest)


def busy_reason() -> str | None:
    """当前有没有会写库的任务在跑。恢复时必须为空。"""
    from . import evolve, library, ocr, scheduler, tasks

    checks = (
        (lambda: bool(tasks.busy_reason()), "后台学习 / 炼化 / 导入 / OCR / 微调任务正在进行"),
        (lambda: evolve.engine.snapshot().get("running"), "自主学习正在进行"),
        (lambda: library.state().get("running"), "书库导入 / 解析正在进行"),
        (lambda: ocr.state().get("running"), "OCR 识别正在进行"),
        (lambda: scheduler.state().get("enabled"), "持续自学处于开启状态"),
    )
    for probe, reason in checks:
        try:
            if probe():
                return reason
        except Exception:  # noqa: BLE001  探测失败不拦恢复，只跳过该项
            continue
    return None


# --------------------------------------------------------------------------
# 备份
# --------------------------------------------------------------------------
def export_backup(label: str = "") -> dict:
    """一键备份：生成一个 zip，返回元信息（含前端下载地址）。"""
    from . import memory

    db_file = Path(config.DB_FILE)
    if not db_file.exists():
        raise FileNotFoundError("知识库还没有生成：先学习一轮或导入一本书")
    base = _dir()
    base.mkdir(parents=True, exist_ok=True)

    stem = f"evomind-backup-{datetime.now():%Y%m%d-%H%M%S}"
    slug = _slug(label)
    if slug:
        stem += f"-{slug}"
    # 同一秒内连点两次会重名：加序号尾数，绝不互相覆盖
    name = f"{stem}.zip"
    n = 1
    while (base / name).exists():
        n += 1
        name = f"{stem}-{n}.zip"

    counts: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="evomind-bak-") as td:
        tdp = Path(td)
        snap = tdp / DB_MEMBER
        counts = _snapshot_db(snap)

        cfg_out = dict(config.load())
        for k in REDACT_KEYS:
            if cfg_out.get(k):
                cfg_out[k] = "<已打码：恢复后请到「设置」重新填写>"
        (tdp / CONFIG_MEMBER).write_text(
            json.dumps(cfg_out, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        created_at = datetime.now()
        manifest = {
            "app": "EvoMind",
            "manifest_version": MANIFEST_VERSION,
            "created_at": created_at.isoformat(timespec="seconds"),
            "created_ts": int(time.time()),
            "label": str(label or "")[:40],
            "source_db": str(db_file),
            "counts": counts,
        }
        (tdp / MANIFEST_MEMBER).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (tdp / README_MEMBER).write_text(_readme(manifest), encoding="utf-8")

        # 先写 .part 再原子改名：中途取消 / 崩溃只会留下 .part，不留半个 zip
        staging = base / (name + ".part")
        try:
            with zipfile.ZipFile(
                staging, "w", zipfile.ZIP_DEFLATED, compresslevel=6
            ) as zf:
                zf.write(snap, DB_MEMBER)
                zf.write(tdp / MANIFEST_MEMBER, MANIFEST_MEMBER)
                zf.write(tdp / CONFIG_MEMBER, CONFIG_MEMBER)
                zf.write(tdp / README_MEMBER, README_MEMBER)
            os.replace(staging, base / name)
        finally:
            staging.unlink(missing_ok=True)

    final = base / name
    pruned = _prune(protect=name)
    try:
        memory.log_event(
            "info",
            f"已创建备份 {name}（{_human(final.stat().st_size)}）",
            {"counts": counts, "pruned": pruned},
        )
    except Exception:  # noqa: BLE001  日志失败不影响备份本身
        pass

    return {
        "ok": True,
        "name": name,
        "size": final.stat().st_size,
        "size_human": _human(final.stat().st_size),
        "counts": counts,
        "created_at": int(time.time()),
        "download": f"/api/backups/download/{name}",
        "pruned": pruned,
    }


def _readme(manifest: dict[str, Any]) -> str:
    c = manifest.get("counts") or {}
    lines = [
        "EvoMind 学习数据备份",
        "=" * 40,
        f"备份时间：{manifest.get('created_at', '—')}",
        f"数据来源：{manifest.get('source_db', '—')}",
        "",
        "内容：knowledge.db（知识点 "
        f"{c.get('knowledge')} 条 / 书 {c.get('books')} 本 / 章节 "
        f"{c.get('book_chapters')} / OCR 页 {c.get('ocr_pages')}）+ config.json（密钥已打码）",
        "",
        "恢复方式一（推荐）：用 EvoMind 应用内置功能",
        "  1. 启动 EvoMind，进入「设置 → 数据备份与恢复」",
        "  2. 「从备份文件恢复」选择本 zip，确认",
        "  3. 恢复前应用会自动把当前库另存一份（备注 pre-restore），可回退",
        "",
        "恢复方式二：手动（新机器 / 新实例）",
        "  1. 解压本 zip，取出 knowledge.db",
        "  2. 覆盖实例目录下的 data/knowledge/knowledge.db",
        "  3. 删掉同目录可能残留的 knowledge.db-wal / knowledge.db-shm，再启动",
        "",
        "注意：本 zip 含你的全部学习数据，请妥善保存，勿公开分享。",
    ]
    return "\n".join(lines) + "\n"


def _prune(protect: str | None = None) -> list[str]:
    """超量时删最旧的，最多留 KEEP_LATEST 份。

    按文件修改时间排序（名字带备注，字典序≠生成顺序，不能用来判新旧）；
    protect 是刚生成的这份，兜底绝不删自己。
    """
    base = _dir()
    if not base.is_dir():
        return []
    files = sorted(
        (p for p in base.iterdir() if p.is_file() and NAME_RE.match(p.name)),
        key=lambda p: p.stat().st_mtime,
    )
    removed = []
    for p in files[: max(0, len(files) - KEEP_LATEST)]:
        if protect and p.name == protect:
            continue
        try:
            p.unlink()
            removed.append(p.name)
        except OSError:
            pass
    return removed


def list_backups() -> list[dict]:
    base = _dir()
    if not base.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for p in base.iterdir():
        if not (p.is_file() and NAME_RE.match(p.name)):
            continue
        st = p.stat()
        info: dict[str, Any] = {
            "name": p.name,
            "size": st.st_size,
            "size_human": _human(st.st_size),
            "created_at": int(st.st_mtime),
            "created_iso": "",
            "label": "",
            "counts": {},
        }
        try:
            with zipfile.ZipFile(p) as zf:
                if MANIFEST_MEMBER in zf.namelist():
                    m = json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
                    info["created_at"] = int(m.get("created_ts") or info["created_at"])
                    info["created_iso"] = str(m.get("created_at") or "")
                    info["label"] = str(m.get("label") or "")
                    info["counts"] = m.get("counts") or {}
        except (OSError, zipfile.BadZipFile, ValueError, KeyError):
            pass  # 半截 / 损坏的 zip 仍列出来，让用户能删掉它
        items.append(info)
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def delete_backup(name: str) -> None:
    p = backup_path(name)
    if not p.exists():
        raise FileNotFoundError(f"备份不存在：{name}")
    p.unlink()


# --------------------------------------------------------------------------
# 恢复
# --------------------------------------------------------------------------
def validate_backup(zip_path: Path) -> dict:
    """只读校验一个备份 zip：结构 + 主库完整性 + 行数，返回概要。"""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise FileNotFoundError(f"备份文件不存在：{zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("不是有效的备份文件（zip 结构不对）")

    manifest: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="evomind-validate-") as td:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if DB_MEMBER not in names:
                raise ValueError("备份里缺少 knowledge.db")
            if MANIFEST_MEMBER in names:
                try:
                    manifest = json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    manifest = {}
            zf.extract(DB_MEMBER, td)
        db = Path(td) / DB_MEMBER
        ok, msg = _quick_check(db)
        if not ok:
            raise ValueError(f"备份里的数据库不完整：{msg}")
        counts = _counts(db)

    st = zip_path.stat()
    return {
        "name": zip_path.name,
        "size": st.st_size,
        "size_human": _human(st.st_size),
        "manifest": manifest,
        "counts": counts,
    }


def restore_backup(zip_path: Path, confirm: bool = False) -> dict:
    """从备份 zip 恢复知识库。

    顺序很重要：先把新库从 zip 里取出来并校验，再给当前库做兜底备份，
    最后原子替换——中途任何一步失败，当前库都还完好。
    """
    from . import memory

    if not confirm:
        raise ValueError("恢复会覆盖当前知识库，请显式确认")
    reason = busy_reason()
    if reason:
        raise RuntimeError(f"{reason}，请先停止该任务再恢复")

    info = validate_backup(zip_path)

    target = Path(config.DB_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 1) 把新库取到和目标同卷的临时目录（os.replace 不能跨卷）
    staging_dir = tempfile.mkdtemp(prefix=".evomind-restore-", dir=str(target.parent))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extract(DB_MEMBER, staging_dir)
        staged = Path(staging_dir) / DB_MEMBER
        ok, msg = _quick_check(staged)
        if not ok:
            raise ValueError(f"备份里的数据库不完整：{msg}")

        # 2) 当前库先留一份可回退的兜底（同样是一致快照）
        pre = export_backup("pre-restore")

        # 3) 清掉可能和新库不匹配的 WAL/SHM 边车，再原子换上
        for side in ("-wal", "-shm", "-journal"):
            Path(str(target) + side).unlink(missing_ok=True)
        os.replace(staged, target)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    try:
        memory.log_event(
            "warn",
            f"已从备份恢复知识库：{info['name']}（旧库存为 {pre['name']}）",
            {"counts": info["counts"]},
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "restored": info["name"],
        "counts": info["counts"],
        "size_human": info["size_human"],
        "pre_backup": pre["name"],
        "msg": "恢复完成，页面数据将在数秒内自动刷新",
    }
