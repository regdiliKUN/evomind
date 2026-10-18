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
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

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


def init_db() -> None:
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
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["excludes"] = json.loads(d.get("excludes") or "[]")
        except Exception:
            d["excludes"] = []
        d["book_count"] = c.execute(
            "SELECT COUNT(*) n FROM books WHERE dir_id=?", (d["id"],)
        ).fetchone()["n"]
        d["imported_count"] = c.execute(
            "SELECT COUNT(*) n FROM books WHERE dir_id=? AND status='done'", (d["id"],)
        ).fetchone()["n"]
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
        excludes = d.get("excludes") or []
        _emit(on_event, "info", f"扫描 {d['label'] or root.name}（排除：{'、'.join(excludes) or '无'}）")

        for p in root.rglob("*"):
            if _task["stop"]:
                break
            try:
                if not p.is_file():
                    continue
                hit = next((x for x in excludes if x and x in str(p)), None)
                if hit:
                    excluded_by_dir[hit] = excluded_by_dir.get(hit, 0) + 1
                    skipped += 1
                    continue
                ext = p.suffix.lower()
                if ext in SKIP_FORMATS or ext not in SUPPORTED:
                    key = ext or "(无扩展名)"
                    unsupported_ext[key] = unsupported_ext.get(key, 0) + 1
                    skipped += 1
                    continue
                found += 1
                exists = c.execute("SELECT id FROM books WHERE path=?", (str(p),)).fetchone()
                if exists:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                c.execute(
                    "INSERT OR IGNORE INTO books(id,dir_id,path,source_type,format,"
                    "title,size_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        hashlib.md5(str(p).encode("utf-8")).hexdigest()[:16],
                        d["id"],
                        str(p),
                        "local",
                        ext.lstrip("."),
                        p.stem,
                        size,
                        "pending",
                        time.time(),
                    ),
                )
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
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "big5", "shift_jis", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return p.read_bytes().decode("utf-8", errors="replace")


def parse_pdf(p: Path) -> dict:
    from pypdf import PdfReader

    r = PdfReader(str(p))
    pages = len(r.pages)
    texts = []
    empty = 0
    for pg in r.pages:
        try:
            t = pg.extract_text() or ""
        except Exception:
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
    return {"ok": True, "chars": len(text), "chapters": len(chapters), "text": text}


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

    # 正文只解析一次：状态还没就绪时 parse_book 会顺带解析并把 text 带回来，
    # 直接就绪的直接读一次。之前两种情况都会再解析一遍，大 PDF 白白多花几十秒。
    if book["status"] not in ("ready", "done"):
        r = parse_book(book_id, on_event)
        if not r.get("ok"):
            return r
        raw_text = r.get("text") or ""
        row = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        book = dict(row)
    else:
        try:
            raw_text = _extract_text(p).get("text", "")
        except Exception as e:  # noqa: BLE001
            c.execute("UPDATE books SET status='failed', error=? WHERE id=?",
                      (str(e)[:300], book_id))
            c.commit()
            return {"ok": False, "msg": str(e)}

    # 主题要和 memory.add_point 里写入的一致（那边会压平空白），
    # 否则 books.topic 与 knowledge.topic 对不上，按主题筛书会漏。
    topic = " ".join((topic or book["topic"] or f"书库 · {book['title']}").split())
    c.execute("UPDATE books SET status='importing', topic=? WHERE id=?", (topic, book_id))
    c.commit()

    if not raw_text:
        c.execute("UPDATE books SET status='ready', error='正文为空' WHERE id=?", (book_id,))
        c.commit()
        return {"ok": False, "msg": "正文为空，无法提炼"}

    chapters = _split_chapters(raw_text, book["title"])
    total_points = 0
    used = 0
    for ci, ch in enumerate(chapters):
        if used >= max_chunks:
            _emit(on_event, "info", f"已达单本上限 {max_chunks} 块，剩余章节跳过")
            break
        for chunk in _chunk(ch["text"]):
            if used >= max_chunks:
                break
            used += 1
            _emit(on_event, "stage", f"《{book['title']}》{ch['title'][:30]} · 第 {used} 块")
            try:
                data = llm.chat_json(
                    EXTRACT_SYSTEM,
                    EXTRACT_USER.format(
                        topic=topic,
                        title=book["title"],
                        chapter=ch["title"][:60],
                        text=chunk[:9000],
                    ),
                    temperature=0.2,
                )
            except Exception as e:  # noqa: BLE001
                _emit(on_event, "warn", f"提炼失败（内部已重试）：{str(e)[:60]}")
                continue
            for pt in (data.get("points") or [])[:6]:
                claim = (pt.get("claim") or "").strip()
                if len(claim) < 12:
                    continue
                try:
                    rel = float(pt.get("relevance", 0.5))
                    conf = float(pt.get("confidence", 0.6))
                except Exception:
                    rel, conf = 0.5, 0.6
                if rel < float(config.get("min_relevance", 0.45)):
                    continue
                try:
                    pid = memory.add_point(
                        topic=topic,
                        claim=claim,
                        detail=(pt.get("detail") or "").strip()[:1200],
                        source_url=f"file:///{str(p).replace(chr(92), '/')}",
                        source_title=f"《{book['title']}》 {ch['title'][:60]}",
                        source_kind="book",
                        tags=pt.get("tags") or [],
                        confidence=max(0.1, min(1.0, conf)),
                        session_id="",
                        vec=None,
                        dedup=True,
                    )
                    if pid:
                        total_points += 1
                        try:
                            c.execute("UPDATE knowledge SET book_id=? WHERE id=?", (book_id, pid))
                        except Exception:
                            pass
                        _emit(on_event, "learn", f"＋ {claim[:70]}")
                except Exception as e:  # noqa: BLE001
                    _emit(on_event, "warn", f"入库失败：{str(e)[:60]}")

    c.execute(
        "UPDATE books SET status='done', points_count=?, imported_at=? WHERE id=?",
        (total_points, time.time(), book_id),
    )
    c.commit()
    _emit(on_event, "ok", f"《{book['title']}》导入完成，新增 {total_points} 条知识点")
    return {"ok": True, "points": total_points, "chunks": used}


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
        total_pts = 0
        for i, bid in enumerate(book_ids):
            if _task["stop"]:
                _emit(on_event, "warn", "收到停止指令，中断导入")
                break
            _task["done"] = i
            row = memory.conn().execute("SELECT title FROM books WHERE id=?", (bid,)).fetchone()
            _task["current"] = row["title"] if row else bid
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
    total = 0
    used = 0
    for ch in chapters:
        for chunk in _chunk(ch["text"]):
            if used >= MAX_CHUNKS_PER_BOOK:
                break
            used += 1
            try:
                data = llm.chat_json(
                    EXTRACT_SYSTEM,
                    EXTRACT_USER.format(topic=topic, title=title,
                                        chapter=ch["title"][:60], text=chunk[:9000]),
                    temperature=0.2,
                )
            except Exception as e:  # noqa: BLE001
                _emit(on_event, "warn", f"提炼失败（内部已重试）：{str(e)[:60]}")
                continue
            for pt in (data.get("points") or [])[:6]:
                claim = (pt.get("claim") or "").strip()
                if len(claim) < 12:
                    continue
                try:
                    rel = float(pt.get("relevance", 0.5))
                    conf = float(pt.get("confidence", 0.6))
                except Exception:
                    rel, conf = 0.5, 0.6
                if rel < float(config.get("min_relevance", 0.45)):
                    continue
                pid = memory.add_point(
                    topic=topic, claim=claim,
                    detail=(pt.get("detail") or "").strip()[:1200],
                    source_url=url, source_title=title,
                    source_kind="web", tags=pt.get("tags") or [],
                    confidence=max(0.1, min(1.0, conf)),
                )
                if pid:
                    total += 1
                    _emit(on_event, "learn", f"＋ {claim[:70]}")
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
    def n(sql: str, *p: Any) -> int:
        return c.execute(sql, p).fetchone()["n"]

    return {
        "dirs": n("SELECT COUNT(*) n FROM library_dirs"),
        "books": n("SELECT COUNT(*) n FROM books"),
        "pending": n("SELECT COUNT(*) n FROM books WHERE status='pending'"),
        "ready": n("SELECT COUNT(*) n FROM books WHERE status='ready'"),
        "done": n("SELECT COUNT(*) n FROM books WHERE status='done'"),
        "skipped": n("SELECT COUNT(*) n FROM books WHERE status='skipped'"),
        "failed": n("SELECT COUNT(*) n FROM books WHERE status='failed'"),
        "points": n("SELECT COUNT(*) n FROM knowledge WHERE book_id IS NOT NULL AND book_id!=''"),
        "by_format": [
            {"format": r["format"], "count": r["n"]}
            for r in c.execute(
                "SELECT format, COUNT(*) n FROM books GROUP BY format ORDER BY n DESC"
            ).fetchall()
        ],
    }


def delete_book(book_id: str, purge_points: bool = False) -> dict:
    c = memory.conn()
    row = c.execute("SELECT path, source_type FROM books WHERE id=?", (book_id,)).fetchone()
    if purge_points:
        try:
            c.execute("DELETE FROM knowledge WHERE book_id=?", (book_id,))
        except Exception:
            pass
    c.execute("DELETE FROM book_chapters WHERE book_id=?", (book_id,))
    c.execute("DELETE FROM books WHERE id=?", (book_id,))
    c.commit()

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
