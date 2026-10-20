"""本地书库：多目录扫描、多格式解析、批量提炼入库。

设计要点：
  1. 目录可配置多个，各自可设排除规则（默认排除「成人」这类不该进知识库的目录）
  2. 扫描与导入分离 —— 先扫出书目清单让你确认，再决定导哪些
  3. 扫描版 PDF 自动识别（提取不到文字层就标 skipped，不浪费时间）
  4. 一本书按章节切块，逐块提炼知识点，入同一个 knowledge 表，靠 book_id 回溯
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

from . import config, fetcher, llm, memory

Event = Callable[[str, str, dict], None]

# 支持的格式：能直接读文本的 / 需要解析的 / 不处理的
TEXT_FORMATS = {".txt", ".md", ".markdown", ".csv"}
DOC_FORMATS = {".pdf", ".epub", ".docx", ".doc", ".htm", ".html", ".rtf"}
SKIP_FORMATS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".mp4", ".mkv", ".avi", ".mov", ".wav", ".mp3", ".flac", ".m4a",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".iso",
    ".exe", ".dll", ".msi", ".apk", ".demo", ".player", ".url", ".ass",
    ".xls", ".xlsx", ".ppt", ".pptx",
}
SUPPORTED = TEXT_FORMATS | DOC_FORMATS

# 一本书最多提炼多少个块（防止一本大部头跑一整天）
MAX_CHUNKS_PER_BOOK = 40
CHUNK_SIZE = 2200
CHUNK_OVERLAP = 200
# 每页少于这么多字，判定为扫描图型 PDF
MIN_CHARS_PER_PAGE = 40

_lock = threading.RLock()
_task = {
    "running": False,
    "kind": "",
    "total": 0,
    "done": 0,
    "current": "",
    "stage": "idle",
    "points": 0,
    "log": [],
    "stop": False,
}


# --------------------------------------------------------------------------
# 数据表
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS library_dirs (
    id TEXT PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    label TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    excludes TEXT DEFAULT '["成人"]',
    last_scan REAL DEFAULT 0,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    dir_id TEXT DEFAULT '',
    path TEXT UNIQUE NOT NULL,
    source_type TEXT DEFAULT 'local',
    format TEXT DEFAULT '',
    title TEXT DEFAULT '',
    author TEXT DEFAULT '',
    size_bytes INTEGER DEFAULT 0,
    chars INTEGER DEFAULT 0,
    pages INTEGER DEFAULT 0,
    text_ratio REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    skip_reason TEXT DEFAULT '',
    points_count INTEGER DEFAULT 0,
    topic TEXT DEFAULT '',
    error TEXT DEFAULT '',
    imported_at REAL DEFAULT 0,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_books_status ON books(status);
CREATE INDEX IF NOT EXISTS idx_books_dir ON books(dir_id);

CREATE TABLE IF NOT EXISTS book_chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT,
    idx INTEGER,
    title TEXT DEFAULT '',
    chars INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chapters_book ON book_chapters(book_id);
"""


#: 建表建索引是「一次性」的事，但 init_db() 挂在 8 个读路径上，每次调用都会
#: 重跑 7 条 DDL + 一条注定失败的 ALTER TABLE（靠抛异常来判断列已存在）+
#: 一条 knowledge 建索引 + 一次 commit。列表/统计这类只读请求本该只付一次
#: SELECT，却先付了九条语句的过路费。按库文件身份记「已建好」，之后只留
#: 一条廉价的 SELECT 自检（库文件被换掉/删掉时表会消失，得能自动重建）。
_INIT_DONE: dict = {"db": None}
#: 压测/对照用：置 True 时 init_db() 退回「每次全量 DDL」的旧路径
_DISABLE_INIT_GUARD = False
#: 压测/对照用：置 True 时 list_dirs() 退回「每目录两条 COUNT」的 N+1 旧路径
_DISABLE_DIRS_TALLY = False
#: 说明：list_books() 的「COUNT + 分页」双跑曾试过用窗口函数
#: COUNT(*) OVER () 合并成一趟，实测反而慢 3 倍多（窗口函数会强制物化
#: 整个结果集再排序，把 ORDER BY ... LIMIT 的 top-N 优化废掉），故保留原样。


def init_db() -> None:
    global _INIT_DONE
    db = str(config.DB_FILE)
    if not _DISABLE_INIT_GUARD and _INIT_DONE["db"] == db:
        try:
            memory.conn().execute("SELECT 1 FROM library_dirs LIMIT 1")
            return
        except Exception:  # noqa: BLE001  库被换掉/删了，落到下面重建
            _INIT_DONE["db"] = None
    c = memory.conn()
    c.executescript(SCHEMA)
    # knowledge 表补 book_id 列（老库升级用，失败说明已存在）
    try:
        c.execute("ALTER TABLE knowledge ADD COLUMN book_id TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_k_book ON knowledge(book_id)")
    except Exception:
        pass
    c.commit()
    _INIT_DONE["db"] = db


def recover_stale_states() -> dict:
    """修复上次异常退出留下的中间态。

    导入时先把状态写成 importing 再逐块提炼，进程如果在中途被杀掉（关窗口、
    断电、Ctrl+C），这本书会永远停在 importing。前端只给 pending / ready
    提供勾选框，于是它既不能继续也不能重来，用户只能去手改数据库。

    启动时没有任务在跑，此时仍处于 importing / parsing 的一定是孤儿：
      - 已经解析出正文的 → ready（可重新导入，语义去重会挡住重复条目）
      - 从没解析过的     → pending
    """
    if _task["running"]:
        return {"importing": 0, "parsing": 0}

    c = memory.conn()
    stale = [
        r["id"]
        for r in c.execute("SELECT id FROM books WHERE status='importing'").fetchall()
    ]
    if stale:
        c.execute(
            "UPDATE books SET status=CASE WHEN COALESCE(chars,0)>0 THEN 'ready'"
            " ELSE 'pending' END WHERE status='importing'"
        )
        # 中断前已入库的条目按 book_id 回填，否则界面显示「0 条知识」
        for bid in stale:
            n = c.execute(
                "SELECT COUNT(*) n FROM knowledge WHERE book_id=?", (bid,)
            ).fetchone()["n"]
            c.execute("UPDATE books SET points_count=? WHERE id=?", (n, bid))

    n_par = c.execute(
        "UPDATE books SET status='pending' WHERE status='parsing'"
    ).rowcount
    c.commit()

    if stale:
        memory.log_event(
            "warn",
            f"上次退出时有 {len(stale)} 本书停在导入中，已恢复为可重新导入",
        )
    return {"importing": len(stale), "parsing": n_par}


def _emit(on_event: Event | None, level: str, msg: str, **payload) -> None:
    _task["log"].append({"ts": time.time(), "level": level, "msg": msg})
    _task["log"] = _task["log"][-300:]
    if on_event:
        on_event(level, msg, payload)


def state() -> dict:
    return {k: v for k, v in _task.items() if k != "log"} | {
        "log": _task["log"][-40:]
    }


# --------------------------------------------------------------------------
# 目录配置
# --------------------------------------------------------------------------
def add_dir(path: str, label: str = "", excludes: list[str] | None = None) -> dict:
    init_db()
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return {"ok": False, "msg": f"目录不存在：{path}"}
    c = memory.conn()
    row = c.execute("SELECT id FROM library_dirs WHERE path=?", (str(p),)).fetchone()
    if row:
        return {"ok": False, "msg": "这个目录已经添加过了", "id": row["id"]}
    did = uuid.uuid4().hex[:12]
    c.execute(
        "INSERT INTO library_dirs(id,path,label,enabled,excludes,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            did,
            str(p),
            label or p.name,
            1,
            json.dumps(excludes if excludes is not None else ["成人"], ensure_ascii=False),
            time.time(),
        ),
    )
    c.commit()
    return {"ok": True, "id": did}


def list_dirs() -> list[dict]:
    c = memory.conn()
    rows = c.execute("SELECT * FROM library_dirs ORDER BY created_at").fetchall()
    # 原来每个目录都发两条 COUNT（总数 + 已导入数），30 个目录就是 61 条语句。
    # 改成一条按 dir_id 聚合，再一次查表把结果贴回去：2 条语句搞定，且与目录数无关。
    if _DISABLE_DIRS_TALLY:
        tally = {}
    else:
        tally = {
            r["dir_id"]: (r["n"], r["done"])
            for r in c.execute(
                "SELECT dir_id, COUNT(*) n,"
                " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done"
                " FROM books GROUP BY dir_id"
            ).fetchall()
        }
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["excludes"] = json.loads(d.get("excludes") or "[]")
        except Exception:
            d["excludes"] = []
        if _DISABLE_DIRS_TALLY:
            d["book_count"] = c.execute(
                "SELECT COUNT(*) n FROM books WHERE dir_id=?", (d["id"],)
            ).fetchone()["n"]
            d["imported_count"] = c.execute(
                "SELECT COUNT(*) n FROM books WHERE dir_id=? AND status='done'", (d["id"],)
            ).fetchone()["n"]
        else:
            n, done = tally.get(d["id"], (0, 0))
            d["book_count"] = n
            d["imported_count"] = done
        out.append(d)
    return out


def update_dir(did: str, **kw) -> dict:
    allowed = {"label", "enabled", "excludes"}
    patch = {k: v for k, v in kw.items() if k in allowed}
    if not patch:
        return {"ok": False, "msg": "没有可更新的字段"}
    if "excludes" in patch and isinstance(patch["excludes"], list):
        patch["excludes"] = json.dumps(patch["excludes"], ensure_ascii=False)
    cols = ",".join(f"{k}=?" for k in patch)
    c = memory.conn()
    c.execute(f"UPDATE library_dirs SET {cols} WHERE id=?", (*patch.values(), did))
    c.commit()
    return {"ok": True}


def remove_dir(did: str, purge_books: bool = False) -> dict:
    c = memory.conn()
    if purge_books:
        c.execute("DELETE FROM books WHERE dir_id=?", (did,))
    c.execute("DELETE FROM library_dirs WHERE id=?", (did,))
    c.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
def _excluded(path: Path, excludes: list[str]) -> bool:
    s = str(path)
    return any(x and x in s for x in excludes)


def _iter_files(root: Path) -> Iterator[os.DirEntry]:
    """递归列出文件。

    用 os.scandir 而不是 Path.rglob：rglob 返回的是 Path，判断是不是文件还得
    再 stat 一次；DirEntry 在 Windows 上直接从目录项带出类型，is_file() 是免费的。
    真实书库动辄几万个条目，省下来的都是系统调用。
    """
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            yield e
                    except OSError:
                        continue
        except OSError:
            continue


def scan(dirs: list[str] | None = None, on_event: Event | None = None) -> dict:
    """扫描目录，把发现的文件登记进 books 表（不解析内容）。"""
    init_db()
    c = memory.conn()
    targets = list_dirs()
    if dirs:
        targets = [d for d in targets if d["id"] in dirs or d["path"] in dirs]

    found = 0
    added = 0
    skipped = 0
    excluded_by_dir: dict[str, int] = {}   # 每个排除规则命中了多少文件
    unsupported_ext: dict[str, int] = {}   # 因格式不支持跳过了多少
    _task.update(running=True, kind="scan", total=0, done=0, stage="scanning",
                 current="", stop=False, points=0)
    _emit(on_event, "stage", "开始扫描书库目录")

    for d in targets:
        if not d["enabled"]:
            continue
        root = Path(d["path"])
        if not root.exists():
            _emit(on_event, "warn", f"目录不存在，跳过：{root}")
            continue
        raw_excludes = d.get("excludes") or []
        excludes = [x for x in raw_excludes if x]   # 空串不参与匹配，同原来的写法
        _emit(on_event, "info",
              f"扫描 {d['label'] or root.name}（排除：{'、'.join(raw_excludes) or '无'}）")

        # 已有路径一次读进内存。原来每发现一个文件就发一条 SELECT id，
        # 几千个文件就是几千次数据库往返——这是扫描阶段最大的一笔固定开销。
        known = {r["path"] for r in c.execute("SELECT path FROM books").fetchall()}

        for e in _iter_files(root):
            if _task["stop"]:
                break
            try:
                sp = e.path                      # str，只算一次
                hit = next((x for x in excludes if x in sp), None)
                if hit:
                    excluded_by_dir[hit] = excluded_by_dir.get(hit, 0) + 1
                    skipped += 1
                    continue
                name = e.name
                dot = name.rfind(".")
                ext = name[dot:].lower() if dot > 0 else ""
                if ext in SKIP_FORMATS or ext not in SUPPORTED:
                    key = ext or "(无扩展名)"
                    unsupported_ext[key] = unsupported_ext.get(key, 0) + 1
                    skipped += 1
                    continue
                found += 1
                if sp in known:
                    continue
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                c.execute(
                    "INSERT OR IGNORE INTO books(id,dir_id,path,source_type,format,"
                    "title,size_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        hashlib.md5(sp.encode("utf-8")).hexdigest()[:16],
                        d["id"],
                        sp,
                        "local",
                        ext.lstrip("."),
                        name[:dot] if dot > 0 else name,
                        size,
                        "pending",
                        time.time(),
                    ),
                )
                known.add(sp)
                added += 1
                if added % 50 == 0:
                    c.commit()
                    _emit(on_event, "info", f"已登记 {added} 个文件…")
            except Exception:
                continue
        c.commit()
        c.execute("UPDATE library_dirs SET last_scan=? WHERE id=?", (time.time(), d["id"]))
        c.commit()

    _task.update(running=False, stage="idle", total=found, done=found)
    msg = f"扫描完成：发现 {found} 个可导入文件，新增 {added} 个，跳过 {skipped} 个"
    _emit(on_event, "ok", msg)
    # 把跳过明细讲清楚，否则用户不知道排除规则到底排掉了什么
    if excluded_by_dir:
        detail = "、".join(f"{k}（{v} 个）" for k, v in excluded_by_dir.items())
        _emit(on_event, "info", f"按排除规则跳过：{detail}")
    if unsupported_ext:
        top = sorted(unsupported_ext.items(), key=lambda x: -x[1])[:6]
        detail = "、".join(f"{k} {v}" for k, v in top)
        _emit(on_event, "info", f"按格式跳过：{detail}")
    return {
        "ok": True, "found": found, "added": added, "skipped": skipped,
        "excluded_by_rule": excluded_by_dir,
        "unsupported_ext": dict(sorted(unsupported_ext.items(), key=lambda x: -x[1])[:12]),
    }


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------
def _split_chapters(text: str, fallback_title: str) -> list[dict]:
    """按常见章节标题切分。切不出来就当单章。"""
    pattern = re.compile(
        r"^\s*(?:第[一二三四五六七八九十百零〇\d]+[章节回卷篇]|Chapter\s+\d+|CHAPTER\s+\d+"
        r"|#{1,3}\s+\S|\d+\.\s+\S{2,30}$)",
        re.M,
    )
    marks = [m.start() for m in pattern.finditer(text)]
    if len(marks) < 2:
        return [{"title": fallback_title, "text": text}]

    chapters = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(text)
        block = text[start:end].strip()
        if len(block) < 200:
            continue
        first_line = block.split("\n", 1)[0].strip()[:80]
        chapters.append({"title": first_line or f"第 {i+1} 节", "text": block})
    return chapters or [{"title": fallback_title, "text": text}]


MAX_TEXT_BYTES = 200 * 1024 * 1024   # 单个文本文件读取上限
_NL_RE = re.compile(r"\r\n|\r")      # 补回文本模式的换行归一（见 _read_text_file）


# --------------------------------------------------------------------------
# 正文缓存
# --------------------------------------------------------------------------
# 解析一本大 PDF 要几秒到几十秒，而「批量解析」和「批量导入」是两步操作：
# 先跑解析把书标成 ready，再勾选导入。原来的 import_book 看到 status=ready
# 就再解析一遍，等于每本书的解析都做了两次。
# 这里把解析出的正文落盘缓存，导入时直接读回；用「大小+修改时间」做指纹，
# 源文件变了就失效重解析，不会读到旧内容。
TEXT_CACHE_DIR = config.DATA / "cache" / "booktext"
TEXT_CACHE_MAX_BYTES = 1024 ** 3     # 缓存总量上限，超了按最久未用淘汰
_TEXT_CACHE_TRIM_EVERY = 20          # 每写这么多本检查一次总量
_text_cache_writes = {"n": 0}


def _text_cache_file(book_id: str) -> Path:
    return TEXT_CACHE_DIR / book_id[:2] / f"{book_id}.txt"


def _text_fingerprint(p: Path) -> str:
    try:
        st = p.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def _text_cache_save(book_id: str, p: Path, text: str) -> None:
    r"""写入正文缓存。首行是指纹，其余是正文。

    PDF 提取出来的正文可能带代理区码位（CID 映射不全时 pypdf 会给出 \udce0
    这类字符），直接用 utf-8 写会抛 UnicodeEncodeError。surrogatepass 能
    原样编解码，读回来和原文本逐字符一致，不会悄悄把正文改掉。
    """
    if not text:
        return
    try:
        f = _text_cache_file(book_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", errors="surrogatepass", newline="\n") as fh:
            fh.write(_text_fingerprint(p))
            fh.write("\n")
            fh.write(text)
        os.replace(tmp, f)          # 原子替换，避免读到写了一半的文件
        _text_cache_maybe_trim()
    except Exception:  # noqa: BLE001  缓存失败不影响主流程
        pass


def _text_cache_load(book_id: str, p: Path) -> str | None:
    """读取正文缓存。指纹对不上（文件被改过）就当没有。"""
    try:
        # newline="" —— 不让通用换行模式把正文里的 \r 悄悄改成 \n
        with open(_text_cache_file(book_id), "r", encoding="utf-8",
                  errors="surrogatepass", newline="") as fh:
            raw = fh.read()
    except Exception:  # noqa: BLE001
        return None
    head, sep, body = raw.partition("\n")
    if not sep or head != _text_fingerprint(p):
        return None
    return body


def _text_cache_maybe_trim() -> None:
    """缓存超上限时淘汰最久未访问的。"""
    with _lock:
        _text_cache_writes["n"] += 1
        if _text_cache_writes["n"] % _TEXT_CACHE_TRIM_EVERY:
            return
    try:
        items = []
        total = 0
        # 缓存是按 <目录>/<id 前 2 位>/<id>.txt 两级存放的，直接扫这两层比
        # rglob 递归遍历快（rglob 会连目录一起 stat）
        for sub in os.scandir(TEXT_CACHE_DIR):
            try:
                if not sub.is_dir(follow_symlinks=False):
                    continue
                for e in os.scandir(sub.path):
                    if not e.name.endswith(".txt"):
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    items.append((st.st_atime, st.st_size, Path(e.path)))
                    total += st.st_size
            except OSError:
                continue
        if total <= TEXT_CACHE_MAX_BYTES:
            return
        items.sort(key=lambda x: x[0])     # 最久未用的先删
        for _, size, f in items:
            if total <= TEXT_CACHE_MAX_BYTES:
                break
            try:
                f.unlink()
                total -= size
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def _read_text_file(p: Path) -> str:
    # Python 的 str 内部是宽字符，实测 36MB 文件读取峰值到 144MB（约 4 倍膨胀）。
    # 不设上限的话，一个几百 MB 的 txt 就能把内存吃光。
    try:
        size = p.stat().st_size
        if size > MAX_TEXT_BYTES:
            raise ValueError(
                f"文件 {size/1024/1024:.0f}MB 超过 {MAX_TEXT_BYTES//1024//1024}MB 上限，请先拆分"
            )
    except OSError:
        pass
    # 字节只读一次，再逐个编码试着解码。原来用 read_text 逐个编码试，
    # 非 UTF-8 的文件（GBK / Big5 很常见）要把整个文件反复读好几遍。
    raw = p.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "big5", "shift_jis", "latin-1"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # read_text 走文本模式，会把 \r\n / \r 统一成 \n；直接解码不会，
        # 这里补回去，保证读出来的内容和原来一模一样
        return _NL_RE.sub("\n", text) if "\r" in text else text
    return p.read_bytes().decode("utf-8", errors="replace")


def parse_pdf(p: Path) -> dict:
    from pypdf import PdfReader

    r = PdfReader(str(p))
    pages = len(r.pages)
    if not pages:
        return {"text": "", "pages": 0, "chars": 0, "text_ratio": 0.0, "scanned": True}

    # 先采样前几页判断有没有文字层。
    # 画集/扫描版整本都没文字，逐页解析 359MB 的图集要十几分钟、吃掉几个 G 内存，
    # 期间进度一直是 0，看起来就像卡死了。采样几页就能定性。
    sample_n = min(8, pages)
    sample: list[str] = []
    for i in range(sample_n):
        try:
            sample.append(r.pages[i].extract_text() or "")
        except Exception:  # noqa: BLE001
            sample.append("")
    sample_ratio = sum(len(t.strip()) for t in sample) / sample_n

    if sample_ratio < MIN_CHARS_PER_PAGE:
        # 采样就判定没文字，剩余页不必再看
        return {
            "text": "",
            "pages": pages,
            "chars": 0,
            "text_ratio": round(sample_ratio, 1),
            "scanned": True,
        }

    # 采样有文字，说明是文字版，才值得解析全书
    texts = sample
    empty = sum(1 for t in sample if len(t.strip()) < 10)
    for i in range(sample_n, pages):
        try:
            t = r.pages[i].extract_text() or ""
        except Exception:  # noqa: BLE001
            t = ""
        if len(t.strip()) < 10:
            empty += 1
        texts.append(t)
    full = "\n".join(texts)
    chars = len(full.strip())
    ratio = (chars / pages) if pages else 0
    return {
        "text": full,
        "pages": pages,
        "chars": chars,
        "text_ratio": round(ratio, 1),
        "scanned": ratio < MIN_CHARS_PER_PAGE,
    }


def parse_epub(p: Path) -> dict:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(p))
    parts = []
    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        try:
            soup = BeautifulSoup(item.get_content(), "lxml")
            for tag in soup(["script", "style"]):
                tag.decompose()
            t = soup.get_text("\n", strip=True)
            if len(t) > 100:
                parts.append(t)
        except Exception:
            continue
    full = "\n\n".join(parts)
    title = ""
    try:
        meta = book.get_metadata("DC", "title")
        title = meta[0][0] if meta else ""
    except Exception:
        pass
    return {"text": full, "pages": 0, "chars": len(full.strip()),
            "text_ratio": 1.0, "scanned": False, "title": title}


def parse_docx(p: Path) -> dict:
    import docx

    d = docx.Document(str(p))
    paras = [x.text for x in d.paragraphs if x.text.strip()]
    full = "\n".join(paras)
    return {"text": full, "pages": 0, "chars": len(full),
            "text_ratio": 1.0, "scanned": False}


def parse_html(p: Path) -> dict:
    raw = _read_text_file(p)
    try:
        import trafilatura

        t = trafilatura.extract(raw) or ""
    except Exception:
        t = ""
    if len(t) < 200:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw, "lxml")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        t = soup.get_text("\n", strip=True)
    return {"text": t, "pages": 0, "chars": len(t),
            "text_ratio": 1.0, "scanned": False}


def _extract_text(p: Path) -> dict:
    """按扩展名提取正文。统一入口，避免多处重复的 if/elif 分支。

    返回 {text, pages, chars, text_ratio, scanned, [unsupported], [title]}
    """
    ext = p.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(p)
    if ext == ".epub":
        return parse_epub(p)
    if ext == ".docx":
        return parse_docx(p)
    if ext in (".htm", ".html"):
        return parse_html(p)
    if ext == ".doc":
        return {"text": "", "pages": 0, "chars": 0, "text_ratio": 0,
                "scanned": False, "unsupported": ".doc 需要先转成 .docx"}
    if ext == ".rtf":
        raw = _read_text_file(p)
        t = re.sub(r"\\[a-z]+\d*\s?|[{}]", "", raw)
        return {"text": t, "pages": 0, "chars": len(t), "text_ratio": 1.0, "scanned": False}
    t = _read_text_file(p)
    return {"text": t, "pages": 0, "chars": len(t), "text_ratio": 1.0, "scanned": False}


def parse_book(book_id: str, on_event: Event | None = None) -> dict:
    """解析一本书，把章节写进 book_chapters，更新状态。

    返回值带 text，调用方（import_book）可以直接复用，
    不必再解析一遍 —— 大 PDF 解析一次就是几秒到几十秒。
    """
    init_db()
    c = memory.conn()
    row = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        return {"ok": False, "msg": "书目不存在"}
    book = dict(row)
    p = Path(book["path"])
    if not p.exists():
        c.execute("UPDATE books SET status='failed', error='文件不存在' WHERE id=?", (book_id,))
        c.commit()
        return {"ok": False, "msg": "文件不存在"}

    c.execute("UPDATE books SET status='parsing' WHERE id=?", (book_id,))
    c.commit()

    try:
        res = _extract_text(p)
    except Exception as e:  # noqa: BLE001
        c.execute("UPDATE books SET status='failed', error=? WHERE id=?",
                  (str(e)[:300], book_id))
        c.commit()
        return {"ok": False, "msg": str(e)}

    if res.get("unsupported"):
        c.execute("UPDATE books SET status='skipped', skip_reason=? WHERE id=?",
                  (res["unsupported"], book_id))
        c.commit()
        return {"ok": False, "msg": res["unsupported"]}

    if res.get("scanned"):
        reason = f"扫描版 PDF（{res['text_ratio']} 字/页），无文字层，需 OCR"
        c.execute(
            "UPDATE books SET status='skipped', skip_reason=?, pages=?, chars=?, text_ratio=? WHERE id=?",
            (reason, res["pages"], res["chars"], res["text_ratio"], book_id),
        )
        c.commit()
        return {"ok": False, "msg": reason, "scanned": True}

    text = res["text"].strip()
    if len(text) < 200:
        c.execute("UPDATE books SET status='skipped', skip_reason='提取到的正文过少' WHERE id=?",
                  (book_id,))
        c.commit()
        return {"ok": False, "msg": "提取到的正文过少"}

    # 存一份原始正文（未 strip），导入时可以不再解析一遍
    _text_cache_save(book_id, p, res["text"])

    chapters = _split_chapters(text, book["title"] or p.stem)
    c.execute("DELETE FROM book_chapters WHERE book_id=?", (book_id,))
    for i, ch in enumerate(chapters):
        c.execute(
            "INSERT INTO book_chapters(book_id,idx,title,chars) VALUES (?,?,?,?)",
            (book_id, i, ch["title"][:120], len(ch["text"])),
        )
    title = res.get("title") or book["title"] or p.stem
    c.execute(
        "UPDATE books SET status='ready', title=?, chars=?, pages=?, text_ratio=? WHERE id=?",
        (title, len(text), res.get("pages", 0), res.get("text_ratio", 0), book_id),
    )
    c.commit()
    _emit(on_event, "info", f"解析完成：{title}（{len(text)} 字 / {len(chapters)} 章）")
    # 把切好的章节一并返回：import_book 原本会拿同一份正文再切一遍
    return {
        "ok": True, "chars": len(text), "chapters": len(chapters), "text": text,
        "chapter_list": chapters,
    }


# --------------------------------------------------------------------------
# 提炼入库
# --------------------------------------------------------------------------
EXTRACT_SYSTEM = """你是知识提炼器。从书籍正文中抽取可独立成立的知识点。

要求：
- claim：一句话结论，具体可验证，不要「很重要」「需要注意」这类空话
- detail：2-4 句补充，保留关键数字、条件、步骤、判据
- 只提炼正文里真实出现的内容，绝不编造
- 操作步骤类的知识，claim 写成「做 X 时应该 Y，因为 Z」的形式
- 与主题无关的目录、致谢、版权页一律丢弃"""

EXTRACT_USER = """## 主题
{topic}

## 书籍
《{title}》 {chapter}

## 正文
{text}

输出 JSON：
{{"points": [{{"claim": "...", "detail": "...", "tags": ["..."], "relevance": 0.0, "confidence": 0.0}}]}}
最多 6 条，按 relevance 从高到低。"""


def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    return fetcher.chunk(text, size=size, overlap=overlap)


def import_book(
    book_id: str,
    *,
    topic: str = "",
    max_chunks: int = MAX_CHUNKS_PER_BOOK,
    on_event: Event | None = None,
) -> dict:
    """把一本书提炼成知识点入库。"""
    init_db()
    c = memory.conn()
    row = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        return {"ok": False, "msg": "书目不存在"}
    book = dict(row)

    p = Path(book["path"])
    ext = p.suffix.lower()

    # 正文只解析一次：状态还没就绪时 parse_book 会顺带解析并把 text 带回来；
    # 已就绪的先查正文缓存，命中就不必再解析一遍（大 PDF 解析一次要几秒到几十秒）。
    chapters: list[dict] | None = None
    if book["status"] not in ("ready", "done"):
        r = parse_book(book_id, on_event)
        if not r.get("ok"):
            return r
        raw_text = r.get("text") or ""
        row = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        book = dict(row)
        # 复用 parse_book 切好的章节，不再切一遍。
        # 单章时标题是兜底标题，而这里用的是（可能被 epub 元数据更新过的）
        # book['title']，对齐一下，保证章节名和原来完全一致。
        cl = r.get("chapter_list") or []
        if cl:
            chapters = ([{"title": book["title"], "text": cl[0]["text"]}]
                        if len(cl) == 1 else cl)
    else:
        raw_text = _text_cache_load(book_id, p)
        if raw_text is None:
            try:
                raw_text = _extract_text(p).get("text", "")
            except Exception as e:  # noqa: BLE001
                c.execute("UPDATE books SET status='failed', error=? WHERE id=?",
                          (str(e)[:300], book_id))
                c.commit()
                return {"ok": False, "msg": str(e)}
            _text_cache_save(book_id, p, raw_text)

    # 主题要和 memory.add_point 里写入的一致（那边会压平空白），
    # 否则 books.topic 与 knowledge.topic 对不上，按主题筛书会漏。
    topic = " ".join((topic or book["topic"] or f"书库 · {book['title']}").split())
    c.execute("UPDATE books SET status='importing', topic=? WHERE id=?", (topic, book_id))
    c.commit()

    if not raw_text:
        c.execute("UPDATE books SET status='ready', error='正文为空' WHERE id=?", (book_id,))
        c.commit()
        return {"ok": False, "msg": "正文为空，无法提炼"}

    if chapters is None:
        chapters = _split_chapters(raw_text, book["title"])
    total_points = 0
    # 先把要处理的块收集起来，才能并发提炼
    blocks: list[dict] = []
    for ch in chapters:
        if len(blocks) >= max_chunks:
            _emit(on_event, "info", f"已达单本上限 {max_chunks} 块，剩余章节跳过")
            break
        for chunk in _chunk(ch["text"]):
            if len(blocks) >= max_chunks:
                break
            blocks.append({"chapter": ch["title"], "text": chunk})

    used = len(blocks)
    if not used:
        c.execute("UPDATE books SET status='ready', error='正文为空' WHERE id=?", (book_id,))
        c.commit()
        return {"ok": False, "msg": "正文为空，无法提炼"}

    _emit(on_event, "stage", f"《{book['title']}》共 {used} 块，开始提炼")

    def _extract(b: dict) -> tuple[dict | None, dict]:
        try:
            data = llm.chat_json(
                EXTRACT_SYSTEM,
                EXTRACT_USER.format(
                    topic=topic,
                    title=book["title"],
                    chapter=b["chapter"][:60],
                    text=b["text"][:9000],
                ),
                temperature=0.2,
            )
            return data, b
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "warn", f"提炼失败（内部已重试）：{str(e)[:60]}")
            return None, b

    done_blocks = 0
    # config.get 每次都要抢一把锁，原来每个知识点都要读一次，提到循环外面
    min_rel = float(config.get("min_relevance", 0.45))
    source_url = f"file:///{str(p).replace(chr(92), '/')}"
    # LLM 调用并发：实测提炼时 GPU 利用率只有 11%，串行等于让显卡空转。
    # 入库仍留在主线程串行，避免并发写库与去重锁互等。
    # 收集所有候选，最后一次性批量入库（见 add_points_batch）：只做一次全库相似度
    # 去重 + 一次事务提交，替代原来「每条一次矩阵乘 + 一次 commit」。
    all_cands: list[dict] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        for data, b in ex.map(_extract, blocks):
            done_blocks += 1
            if data is None:
                continue
            _emit(
                on_event, "stage",
                f"《{book['title']}》{b['chapter'][:30]} · {done_blocks}/{used} 块",
            )
            # 先把这一块里够格的候选挑出来，好一次性把向量算完
            cands: list[tuple[str, str, float, Any]] = []
            for pt in (data.get("points") or [])[:6]:
                claim = (pt.get("claim") or "").strip()
                if len(claim) < 12:
                    continue
                try:
                    rel = float(pt.get("relevance", 0.5))
                    conf = float(pt.get("confidence", 0.6))
                except Exception:
                    rel, conf = 0.5, 0.6
                if rel < min_rel:
                    continue
                detail = (pt.get("detail") or "").strip()[:1200]
                cands.append((claim, detail, max(0.1, min(1.0, conf)),
                              pt.get("tags") or []))
            if not cands:
                continue

            # 向量一次算一整块。原来每条知识点单独调一次 llm.embed，
            # 实测逐条 30.6ms/条、批量(16) 9.2ms/条；批量与逐条的结果逐位相同。
            try:
                vecs = llm.embed([f"{c[0]} {c[1][:400]}" for c in cands])
            except Exception:  # noqa: BLE001  向量化失败就退回让 add_point 自己算
                vecs = []

            for i, (claim, detail, conf, tags) in enumerate(cands):
                all_cands.append({
                    "topic": topic,
                    "claim": claim,
                    "detail": detail,
                    "source_url": source_url,
                    "source_title": f"《{book['title']}》 {b['chapter'][:60]}",
                    "source_kind": "book",
                    "tags": tags,
                    "confidence": conf,
                    "vec": vecs[i] if i < len(vecs) else None,
                })

    if all_cands:
        try:
            res = memory.add_points_batch(all_cands, book_id=book_id)
            total_points = res["added"]
            for claim in res["accepted"]:
                _emit(on_event, "learn", f"＋ {claim[:70]}")
            if res["dup"]:
                _emit(on_event, "info",
                      f"候选 {len(all_cands)} 条，新增 {res['added']} 条，重复跳过 {res['dup']} 条")
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "warn", f"批量入库失败：{str(e)[:60]}")

    c.execute(
        "UPDATE books SET status='done', points_count=?, imported_at=? WHERE id=?",
        (total_points, time.time(), book_id),
    )
    c.commit()
    _emit(on_event, "ok", f"《{book['title']}》导入完成，新增 {total_points} 条知识点")
    return {"ok": True, "points": total_points, "chunks": used}


def _book_meta(ids: list[str]) -> dict[str, tuple[int, str]]:
    """一次取回多本书的 (体积, 标题)。

    原来排序要按 id 逐本查体积、循环里再逐本查标题，等于两轮 N 次查询；
    批量处理几千本时，这些往返本身要好几秒。这里分批一次取回。
    """
    out: dict[str, tuple[int, str]] = {}
    if not ids:
        return out
    c = memory.conn()
    for i in range(0, len(ids), 500):
        part = ids[i : i + 500]
        ph = ",".join("?" * len(part))
        for r in c.execute(
            f"SELECT id,size_bytes,title FROM books WHERE id IN ({ph})", part
        ):
            out[r["id"]] = (r["size_bytes"] or 0, r["title"] or "")
    return out


def parse_books_async(book_ids: list[str], *, on_event: Event | None = None) -> dict:
    """后台批量解析：只提取正文、更新状态，不做 LLM 提炼。

    比「批量导入」快两个数量级——导入每本要十几分钟（40 块 × 20 秒），
    解析每本只要几秒。三千多本书过一遍用这个，几小时能跑完；
    用导入则要一个月。跑完能清楚看出哪些书有文字层、哪些是扫描版。
    """
    ids = [x for x in (book_ids or []) if x]
    if not ids:
        return {"ok": False, "msg": "没有选中要解析的书"}

    with _lock:
        if _task["running"]:
            return {"ok": False, "msg": "已有书库任务在跑"}

    try:
        from . import ocr as _ocr

        if _ocr.state().get("running"):
            return {"ok": False, "msg": "OCR 任务正在跑，等它结束再解析"}
    except Exception:
        pass

    def go() -> None:
        _task.update(running=True, kind="parse", total=len(ids), done=0,
                     stage="parsing", points=0, stop=False, current="")
        # 按体积升序：小书先跑，进度很快就能动起来。
        # 一上来就啃 300MB 的图集，看起来像卡死，实际在慢慢解析。
        meta = _book_meta(ids)
        ordered = sorted(ids, key=lambda b: meta.get(b, (0, ""))[0])
        ok = scan = bad = 0
        for i, bid in enumerate(ordered):
            if _task["stop"]:
                _emit(on_event, "warn", "收到停止指令，中断解析")
                break
            _task["done"] = i
            _task["current"] = meta.get(bid, (0, ""))[1] or bid
            try:
                r = parse_book(bid)
            except Exception:  # noqa: BLE001
                bad += 1
                continue
            if r.get("ok"):
                ok += 1
            elif "扫描版" in str(r.get("msg", "")):
                scan += 1
            else:
                bad += 1
            # 每本都发日志会刷屏，20 本汇总一次
            if (i + 1) % 20 == 0:
                _emit(on_event, "info",
                      f"解析进度 {i+1}/{len(ids)}：可读 {ok}，扫描版 {scan}，失败 {bad}")
        _task.update(running=False, stage="idle", done=len(ids))
        _emit(on_event, "ok",
              f"批量解析完成：可读 {ok} 本，扫描版 {scan} 本，失败 {bad} 本")

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True, "msg": f"已开始解析 {len(ids)} 本（只提取正文，不做提炼）"}


def import_books_async(
    book_ids: list[str],
    *,
    topic: str = "",
    max_chunks: int = MAX_CHUNKS_PER_BOOK,
    on_event: Event | None = None,
) -> dict:
    """后台批量导入。"""
    with _lock:
        if _task["running"]:
            return {"ok": False, "msg": "已有书库任务在跑"}

    # OCR 与导入都是重任务，同时跑会互相拖慢
    try:
        from . import finetune as _ft, ocr as _ocr

        if _ocr.state().get("running"):
            return {"ok": False, "msg": "OCR 任务正在跑，等它结束再导入"}
        if _ft.state().get("running"):
            return {"ok": False, "msg": "微调训练正在跑，等它结束再导入"}
    except Exception:
        pass

    # 磁盘闸门：知识点要写数据库，空间耗尽会写失败
    try:
        free = config.free_disk_gb()
        if free < 1:
            return {"ok": False, "msg": f"磁盘剩余 {free}GB，空间不足，先清理再导入"}
    except Exception:  # noqa: BLE001
        pass

    def go() -> None:
        _task.update(running=True, kind="import", total=len(book_ids), done=0,
                     stage="importing", points=0, stop=False, current="")
        # 同样按体积升序，避免第一本是大图集导致进度长时间停在 0
        meta = _book_meta(list(book_ids))
        total_pts = 0
        for i, bid in enumerate(sorted(book_ids, key=lambda b: meta.get(b, (0, ""))[0])):
            if _task["stop"]:
                _emit(on_event, "warn", "收到停止指令，中断导入")
                break
            _task["done"] = i
            _task["current"] = meta.get(bid, (0, ""))[1] or bid
            try:
                r = import_book(bid, topic=topic, max_chunks=max_chunks, on_event=on_event)
                total_pts += r.get("points", 0)
                _task["points"] = total_pts
            except Exception as e:  # noqa: BLE001
                _emit(on_event, "error", f"导入失败 {bid}: {str(e)[:80]}")
        _task.update(running=False, stage="idle", done=len(book_ids))
        _emit(on_event, "ok", f"批量导入完成：{len(book_ids)} 本，共 {total_pts} 条知识点")

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


def stop_task() -> dict:
    _task["stop"] = True
    return {"ok": True}


# --------------------------------------------------------------------------
# 链接导入
# --------------------------------------------------------------------------
def import_url(url: str, topic: str = "", on_event: Event | None = None) -> dict:
    """从用户提供的链接获取内容并入库。

    - 直链电子书（.pdf/.epub/.txt）→ 下载后按文件解析
    - 普通网页 → 提取正文
    只处理用户明确提供、且可公开访问的地址。
    """
    init_db()
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "msg": "只支持 http/https 链接"}

    _emit(on_event, "info", f"获取链接内容：{url[:80]}")

    # 判断是不是电子书直链
    lower = url.lower().split("?")[0]
    ext = Path(lower).suffix
    if ext in DOC_FORMATS or ext in TEXT_FORMATS:
        try:
            import httpx

            with httpx.Client(
                timeout=120, follow_redirects=True, trust_env=False,
                headers={"User-Agent": config.get("user_agent")},
            ) as c:
                r = c.get(url)
                if r.status_code != 200:
                    return {"ok": False, "msg": f"下载失败 HTTP {r.status_code}"}
                if len(r.content) > 200 * 1024 * 1024:
                    return {"ok": False, "msg": "文件超过 200MB，拒绝下载"}
                target_dir = config.DATA / "library"
                target_dir.mkdir(parents=True, exist_ok=True)
                name = Path(lower).name or f"download{ext}"
                dest = target_dir / f"{hashlib.md5(url.encode()).hexdigest()[:10]}_{name}"
                dest.write_bytes(r.content)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"下载失败：{str(e)[:120]}"}

        bid = hashlib.md5(str(dest).encode("utf-8")).hexdigest()[:16]
        c = memory.conn()
        c.execute(
            "INSERT OR REPLACE INTO books(id,dir_id,path,source_type,format,title,"
            "size_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, "", str(dest), "url", ext.lstrip("."), dest.stem,
             dest.stat().st_size, "pending", time.time()),
        )
        c.commit()
        _emit(on_event, "ok", f"已下载：{dest.name}")
        return import_book(bid, topic=topic, on_event=on_event) | {"book_id": bid}

    # 普通网页
    doc = fetcher.fetch(url)
    if not doc.get("ok"):
        return {"ok": False, "msg": doc.get("error") or "正文提取失败"}

    title = doc.get("title") or url
    topic = topic or f"网页 · {title[:40]}"
    text = doc["text"]
    chapters = _split_chapters(text, title)

    # 先把要处理的块收齐（上限 MAX_CHUNKS_PER_BOOK），才能并发提炼。
    # 原来这里是一条接一条串行调模型，网页正文长的时候要等很久。
    blocks: list[dict] = []
    for ch in chapters:
        for chunk in _chunk(ch["text"]):
            if len(blocks) >= MAX_CHUNKS_PER_BOOK:
                break
            blocks.append({"chapter": ch["title"], "text": chunk})

    def _extract(b: dict) -> dict | None:
        try:
            return llm.chat_json(
                EXTRACT_SYSTEM,
                EXTRACT_USER.format(topic=topic, title=title,
                                    chapter=b["chapter"][:60], text=b["text"][:9000]),
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "warn", f"提炼失败（内部已重试）：{str(e)[:60]}")
            return None

    min_rel = float(config.get("min_relevance", 0.45))
    # 收集所有候选，最后一次性批量入库（只做一次全库相似度 + 一次事务），
    # 替代原来逐条 add_point（每条一次矩阵乘 + 一次 commit）。
    all_cands: list[dict] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        for data in ex.map(_extract, blocks):
            if data is None:
                continue
            cands: list[tuple[str, str, float, Any]] = []
            for pt in (data.get("points") or [])[:6]:
                claim = (pt.get("claim") or "").strip()
                if len(claim) < 12:
                    continue
                try:
                    rel = float(pt.get("relevance", 0.5))
                    conf = float(pt.get("confidence", 0.6))
                except Exception:
                    rel, conf = 0.5, 0.6
                if rel < min_rel:
                    continue
                detail = (pt.get("detail") or "").strip()[:1200]
                cands.append((claim, detail, max(0.1, min(1.0, conf)),
                              pt.get("tags") or []))
            if not cands:
                continue
            # 向量一次算一批，别一条一条往返
            try:
                vecs = llm.embed([f"{c[0]} {c[1][:400]}" for c in cands])
            except Exception:  # noqa: BLE001
                vecs = []
            for i, (claim, detail, conf, tags) in enumerate(cands):
                all_cands.append({
                    "topic": topic, "claim": claim, "detail": detail,
                    "source_url": url, "source_title": title,
                    "source_kind": "web", "tags": tags, "confidence": conf,
                    "vec": vecs[i] if i < len(vecs) else None,
                })

    total = 0
    if all_cands:
        try:
            res = memory.add_points_batch(all_cands)
            total = res["added"]
            for claim in res["accepted"]:
                _emit(on_event, "learn", f"＋ {claim[:70]}")
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "warn", f"批量入库失败：{str(e)[:60]}")
    _emit(on_event, "ok", f"链接导入完成：{title[:40]}，新增 {total} 条")
    return {"ok": True, "points": total, "title": title}


# --------------------------------------------------------------------------
# 书目查询
# --------------------------------------------------------------------------
def list_books(
    *, status: str | None = None, dir_id: str | None = None,
    fmt: str | None = None, q: str | None = None, limit: int = 200, offset: int = 0,
) -> dict:
    init_db()
    # SQLite 的 LIMIT -1 表示「不限制」，负数/超大值必须夹紧，
    # 否则前端传错参数会一次性拉回整张表
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    sql = "SELECT * FROM books WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if dir_id:
        sql += " AND dir_id=?"
        params.append(dir_id)
    if fmt:
        sql += " AND format=?"
        params.append(fmt)
    if q:
        sql += " AND (title LIKE ? OR path LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    total = memory.conn().execute(
        sql.replace("SELECT *", "SELECT COUNT(*) n"), params
    ).fetchone()["n"]
    sql += " ORDER BY (points_count>0) DESC, chars DESC, title LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = memory.conn().execute(sql, params).fetchall()
    return {"items": [dict(r) for r in rows], "total": total}


def book_detail(book_id: str) -> dict:
    init_db()
    c = memory.conn()
    row = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        return {}
    d = dict(row)
    d["chapters"] = [
        dict(r) for r in c.execute(
            "SELECT * FROM book_chapters WHERE book_id=? ORDER BY idx", (book_id,)
        ).fetchall()
    ]
    try:
        d["points"] = [
            dict(r) for r in c.execute(
                "SELECT id,topic,claim,source_title,created_at FROM knowledge "
                "WHERE book_id=? ORDER BY created_at DESC LIMIT 50", (book_id,)
            ).fetchall()
        ]
    except Exception:
        d["points"] = []
    return d


def library_stats() -> dict:
    init_db()
    c = memory.conn()
    # 原来七个状态各发一条 COUNT，界面每刷一次就是八次往返。
    # 合成一条标量子查询：一次往返拿回全部计数。
    row = c.execute(
        "SELECT"
        " (SELECT COUNT(*) FROM library_dirs) AS dirs,"
        " (SELECT COUNT(*) FROM books) AS books,"
        " (SELECT COUNT(*) FROM books WHERE status='pending') AS pending,"
        " (SELECT COUNT(*) FROM books WHERE status='ready') AS ready,"
        " (SELECT COUNT(*) FROM books WHERE status='done') AS done,"
        " (SELECT COUNT(*) FROM books WHERE status='skipped') AS skipped,"
        " (SELECT COUNT(*) FROM books WHERE status='failed') AS failed,"
        " (SELECT COUNT(*) FROM knowledge"
        "   WHERE book_id IS NOT NULL AND book_id!='') AS points"
    ).fetchone()
    return {
        "dirs": row["dirs"],
        "books": row["books"],
        "pending": row["pending"],
        "ready": row["ready"],
        "done": row["done"],
        "skipped": row["skipped"],
        "failed": row["failed"],
        "points": row["points"],
        "by_format": [
            {"format": r["format"], "count": r["n"]}
            for r in c.execute(
                "SELECT format, COUNT(*) n FROM books GROUP BY format ORDER BY n DESC"
            ).fetchall()
        ],
    }


def delete_book(book_id: str, purge_points: bool = False) -> dict:
    with memory._lock:
        c = memory.conn()
        row = c.execute("SELECT path, source_type FROM books WHERE id=?", (book_id,)).fetchone()
        with c:
            if purge_points:
                c.execute("DELETE FROM knowledge WHERE book_id=?", (book_id,))
            c.execute("DELETE FROM book_chapters WHERE book_id=?", (book_id,))
            c.execute("DELETE FROM books WHERE id=?", (book_id,))
        if purge_points:
            memory._invalidate()

    # 链接导入的文件是我们自己下载到 data/library 的，删书目时一并清掉，
    # 否则这个目录只增不减。用户自己书库里的文件绝不碰。
    removed_file = False
    if row and row["source_type"] == "url":
        try:
            p = Path(row["path"])
            lib_dir = (config.DATA / "library").resolve()
            if p.exists() and lib_dir in p.resolve().parents:
                p.unlink()
                removed_file = True
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "removed_file": removed_file}
