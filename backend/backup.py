"""EvoMind 学习数据备份 / 恢复。

一键备份把 SQLite 主库打成自包含 zip：

    knowledge.db   在线一致性快照（引擎正在写也能导，见 _snapshot_db）
    config.json    当前配置（密钥类字段打码）
    manifest.json  备份时间、各表行数、来源信息、schema/模型/代码版本、成员 SHA256
    README.txt     手动恢复步骤（新机器 / 新实例兜底）

恢复走「先校验、再自动兜底备份、最后原子替换」，全程可回退；
引擎 / 书库 / OCR / 持续自学有任务在跑时直接拒绝，避免把正在写入的库换掉。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
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

MANIFEST_VERSION = 2


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _schema_version(db: Path) -> int | None:
    """从快照读知识生命周期 schema 版本；老库没有 schema_meta 表，记作 1。"""
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            r = con.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return int(r[0]) if r else 1
        finally:
            con.close()
    except Exception:  # noqa: BLE001  快照不可读时不影响备份本身
        return None


def _code_fingerprint() -> str:
    """backend/*.py 的内容指纹：备份能自证「它是被哪版代码写出来的」。

    不依赖 git（用户可能是解压即用、没有 .git），按文件名排序保证确定性。
    """
    try:
        root = Path(__file__).resolve().parent
        h = hashlib.sha256()
        for f in sorted(root.glob("*.py")):
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
        return h.hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


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


#: 恢复进行中标志：从准入检查到 os.replace 横跨解压校验与兜底打包，
#: 没有互斥时新任务可在换库窗口内启动并继续往旧 inode 写。
_restoring = threading.Event()


def busy_reason(include_restoring: bool = True) -> str | None:
    """当前有没有会写库的任务在跑。恢复时必须为空。"""
    from . import evolve, library, ocr, scheduler, tasks

    if include_restoring and _restoring.is_set():
        return "已有备份恢复正在进行"
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
        except Exception as e:  # noqa: BLE001  恢复会整体换库：探测失败必须拒绝
            from . import memory
            memory.log_event("warn", f"恢复准入探测失败：{type(e).__name__}: {e}")
            return f"任务状态探测失败（{type(e).__name__}），拒绝恢复"
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
        cfg_path = tdp / CONFIG_MEMBER
        cfg_path.write_text(
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
            # 恢复时能否正确解释 knowledge.db，取决于 schema / 嵌入模型 / 代码版本。
            # 原来这三项都没记，跨版本恢复只能靠猜。
            "schema_version": _schema_version(snap),
            "embedding_model": str(cfg_out.get("embed_model") or ""),
            "model": str(cfg_out.get("model") or ""),
            "code_fingerprint": _code_fingerprint(),
            # 成员 SHA256 让「zip 被改过 / 传输出错」在恢复前就能被发现，
            # 而不只是靠 SQLite 自己的 quick_check。
            "members": {
                DB_MEMBER: {"sha256": _sha256(snap), "bytes": snap.stat().st_size},
                CONFIG_MEMBER: {"sha256": _sha256(cfg_path), "bytes": cfg_path.stat().st_size},
            },
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
        f"schema 版本：{manifest.get('schema_version', '—')} · 嵌入模型：{manifest.get('embedding_model') or '—'}",
        f"代码指纹：{manifest.get('code_fingerprint') or '—'}",
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
def _member_sha256(manifest: dict[str, Any], member: str) -> str | None:
    """manifest 里登记的成员 SHA256；v1 备份或字段残缺时返回 None（兼容跳过）。"""
    meta = (manifest.get("members") or {}).get(member)
    if not isinstance(meta, dict) or not meta.get("sha256"):
        return None
    return str(meta["sha256"])


def _extract_db_verified(zf: zipfile.ZipFile, manifest: dict[str, Any], dest_dir: str | Path) -> Path:
    """把主库解压到 dest_dir，边写边算 SHA256 并与 manifest 比对。

    原来是「先流式解压一遍只为算哈希，再 zf.extract 解压第二遍」；恢复流程还会在
    staging 里解压第三遍。大库（真实规模约 100 MB）每多一遍就多一次完整 inflate。
    这里一次完成：写盘的字节就是被校验的字节。哈希不符时由调用方清理目录。
    """
    dest = Path(dest_dir) / DB_MEMBER
    want = _member_sha256(manifest, DB_MEMBER)
    h = hashlib.sha256()
    with zf.open(DB_MEMBER) as src, dest.open("wb") as out:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            h.update(chunk)
            out.write(chunk)
    if want and h.hexdigest() != want:
        raise ValueError(f"备份成员校验失败：{DB_MEMBER} 与 manifest 记录的 SHA256 不一致")
    return dest


def _validate_into(zip_path: Path, dest_dir: str | Path) -> tuple[dict, Path]:
    """校验备份并把主库解压到 dest_dir：结构 → 成员 SHA256 → quick_check → 行数。

    返回 (概要, 解压出的主库路径)。恢复流程直接把这个路径原子替换上去，
    不必再解压、再 quick_check 一遍。
    """
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise FileNotFoundError(f"备份文件不存在：{zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("不是有效的备份文件（zip 结构不对）")

    manifest: dict[str, Any] = {}
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if DB_MEMBER not in names:
            raise ValueError("备份里缺少 knowledge.db")
        if MANIFEST_MEMBER in names:
            try:
                manifest = json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        # v2 备份逐成员校验；v1 没有 members 字段，按兼容处理跳过。
        # 主库在下面解压时顺带校验，这里只流式核对其余小成员。
        known = set(names)
        for member in (manifest.get("members") or {}):
            if not isinstance(member, str) or member == DB_MEMBER or member not in known:
                continue
            want = _member_sha256(manifest, member)
            if not want:
                continue
            h = hashlib.sha256()
            with zf.open(member) as src:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != want:
                raise ValueError(f"备份成员校验失败：{member} 与 manifest 记录的 SHA256 不一致")
        db = _extract_db_verified(zf, manifest, dest_dir)
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
    }, db


def validate_backup(zip_path: Path) -> dict:
    """只读校验一个备份 zip：结构 + 主库完整性 + 行数，返回概要。"""
    with tempfile.TemporaryDirectory(prefix="evomind-validate-") as td:
        info, _ = _validate_into(zip_path, td)
    return info


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
    if not Path(zip_path).exists():
        raise FileNotFoundError(f"备份文件不存在：{zip_path}")

    target = Path(config.DB_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 解压校验 + 兜底打包耗时数秒到数十秒：这期间必须阻止新任务准入，
    # 并在真正换库前再复核一次，消除「准入通过但任务随后启动」的窗口。
    if _restoring.is_set():
        raise RuntimeError("已有备份恢复正在进行")
    _restoring.set()
    staging_dir = None
    try:
        # 校验直接在与主库同盘的 staging 里做：解压一次（边写边算 SHA256）、
        # quick_check 一次，产物就是待替换的文件。原流程「校验时解压到系统临时
        # 目录 → 再解压到 staging → 再 quick_check」对同一成员 inflate 三遍。
        staging_dir = tempfile.mkdtemp(prefix=".evomind-restore-", dir=str(target.parent))
        info, staged = _validate_into(zip_path, staging_dir)

        pre = export_backup("pre-restore")

        reason = busy_reason(include_restoring=False)
        if reason:
            raise RuntimeError(f"{reason}，请先停止该任务再恢复")

        method = _swap_in(staged, target)
        memory.bump_db_generation()
        _invalidate_caches()
    finally:
        _restoring.clear()
        if staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)

    try:
        memory.log_event(
            "warn",
            f"已从备份恢复知识库：{info['name']}（旧库存为 {pre['name']}）",
            {"counts": info["counts"], "method": method},
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "restored": info["name"],
        "counts": info["counts"],
        "size_human": info["size_human"],
        "pre_backup": pre["name"],
        "method": method,
        "msg": "恢复完成，页面数据将在数秒内自动刷新",
    }


#: 在线覆盖时等待主库锁的上限：恢复准入已保证没有写任务，只剩短读事务。
ONLINE_RESTORE_TIMEOUT = 60.0


def _swap_in(staged: Path, target: Path) -> str:
    """把校验过的 staged 库换成主库，返回所用方式（"replace" / "online"）。

    首选删 -wal/-shm 后 os.replace（零拷贝）。但在 Windows 上，只要还有任何
    连接开着主库——运行中的应用里，线程池每个处理过请求的线程都各持一条
    空闲的线程局部连接——SQLite 打开文件时不带 FILE_SHARE_DELETE，删 -wal、
    替换主库都会 PermissionError（WinError 32），恢复必然失败（实测复现）。
    这时改走 SQLite 在线备份 API：在 SQLite 自己的锁协议下把全部页写进现有
    主库，单事务提交、失败整体回滚，其他连接下次读取即看到新内容。
    """
    try:
        for side in ("-wal", "-shm", "-journal"):
            Path(str(target) + side).unlink(missing_ok=True)
        os.replace(staged, target)
        return "replace"
    except PermissionError:
        try:
            _copy_into_live(staged, target)
        except (sqlite3.Error, TimeoutError) as e:
            # 在线备份失败时目标事务已回滚，当前库保持原样；转成 RuntimeError
            # 让接口按「冲突」返回可读提示，而不是 500。
            raise RuntimeError(f"主库正被占用，无法覆盖：{e}") from e
        return "online"


def _copy_into_live(staged: Path, target: Path, timeout: float | None = None) -> None:
    limit = ONLINE_RESTORE_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit

    def progress(_status: int, _remaining: int, _total: int) -> None:
        # CPython 在 BUSY/LOCKED 时会无限重试 backup_step；抛异常即中止并回滚。
        if time.monotonic() > deadline:
            raise TimeoutError(f"主库持续被占用超过 {limit:.0f} 秒，恢复已中止，当前库未改动")

    src = sqlite3.connect(str(staged))
    try:
        dst = sqlite3.connect(str(target), timeout=15.0)
        try:
            src.backup(dst, pages=-1, progress=progress)
            try:
                # 全量写进 WAL 后立刻回写主文件并截断，不让 -wal 长时间与库等大
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        finally:
            dst.close()
    finally:
        src.close()


def _invalidate_caches() -> None:
    """换库后作废进程内的派生缓存。

    原流程只 bump 连接代次：向量矩阵缓存、检索结果缓存、写入版本号、词法 /
    向量 ANN 索引都还停在旧库上——恢复后检索会拿旧 rowid 去新库里取行。
    各项尽力而为：任何一项失败都不能让已完成的恢复报错。
    """
    from . import memory

    for fn in (memory._invalidate, memory._bump_search_cache, memory._bump_version):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
    for mod in ("search_index", "vector_ann"):
        try:
            import importlib
            importlib.import_module(f"{__package__}.{mod}").mark_stale()
        except Exception:  # noqa: BLE001
            pass
