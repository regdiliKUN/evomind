"""17,698 条无引文历史知识的分层复核流水线。

为什么不是「一键联网验证」
--------------------------
真实库只读统计（2026-11-11）：

===========================  ======  ==========================================
分层                         条数    能做什么
===========================  ======  ==========================================
local_quote                 17,488  source_kind=book、有 book_id，且 ocr_pages
                                    里确有原文；只能**本地补证**，与联网无关
url_recheck                    209  qa/web/wiki，去重后仅 50 个 http URL；
                                    可做来源可达性复检
unresolvable                    1  无 book_id、无 URL，只能重新采集
===========================  ======  ==========================================

也就是说 98.8% 的待复核知识根本没有「原网址」可回查；把它们送去做
「联网事实核查」既拿不到原文、又无法逐字校验。更关键的是，随机 300 条
本地补证抽测里只有 39 条能用 claim/detail 的 16 字窗口逐字命中 OCR 文本
（约 13%）——历史知识大多是模型改写过的转述，不能靠字符串回填冒充证据。

因此这里提供的是一条**可审计、可分批、可断点续跑**的流水线，而不是一个
把 17,698 条一键标成 verified 的按钮：

* ``plan()``       纯只读，兼容「未迁移的 13 列老库」与迁移后的库，产出分层与可行性；
* ``enqueue()``    把待复核项落进 review_queue（默认 dry_run，不写库）；
* ``run_local()``  逐字在本地 OCR 原文里找引文，命中才补 evidence + locator，
                   标记 ``verification='legacy_literal'``，**绝不改成 verified**；
* ``run_urls()``   对去重后的 http 来源做可达性复检，默认 dry_run 不发请求；
* ``status()``     队列进度与证据覆盖率。

所有会写库的动作默认 ``dry_run=True``；状态机里没有「自动标 verified」这一步，
补到引文只代表「有可核对的原文片段」，事实是否成立仍需独立模型核查。
"""
from __future__ import annotations

import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

from sqlite3 import dbapi2 as _dbapi2

from . import config, evidence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    pid TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    book_id TEXT DEFAULT '',
    url TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    state TEXT DEFAULT 'pending',
    note TEXT DEFAULT '',
    enqueued_at REAL,
    reviewed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_review_tier_state ON review_queue(tier, state);
"""

TIERS = ("local_quote", "url_recheck", "unresolvable")
_QUOTE_MIN = 12
_DEFAULT_PAGES = 12


def _ro_conn():
    """只读连主库。

    ``plan()`` 必须能在**尚未迁移的真实老库**上跑：连 memory.conn() 会触发
    executescript + additive 迁移 + 每日维护，那就是写库了。这里用独立的
    ``mode=ro`` 连接，且走 dbapi2.connect 以免测试里 patch sqlite3.connect
    把它记进连接表后在别的线程被关闭。
    """
    p = Path(config.DB_FILE).resolve()
    if not p.exists():
        return None
    try:
        c = _dbapi2.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30.0)
        c.row_factory = sqlite3.Row
        return c
    except Exception:  # noqa: BLE001  库不存在 / 被锁：plan 返回 unavailable
        return None


def _no_evidence_where(c) -> tuple[str, bool]:
    """老库用 detail 里的历史引文锚点判定；新库直接查 evidence 列。"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(knowledge)")}
    migrated = "evidence" in cols
    if migrated:
        where = "COALESCE(evidence,'')=''"
        if "status" in cols:
            where += " AND " + evidence.ACTIVE_FILTER
    else:
        where = f"INSTR(COALESCE(detail,''),'{evidence.EVIDENCE_ANCHOR}')=0"
    return where, migrated


def _tier(row: dict) -> str:
    if str(row.get("source_url") or "").lower().startswith("http"):
        return "url_recheck"
    if str(row.get("book_id") or "").strip():
        return "local_quote"
    return "unresolvable"


def plan(sample: int = 6) -> dict:
    """纯只读分层盘点。不建表、不写库、不触发迁移。"""
    c = _ro_conn()
    if c is None:
        return {"available": False, "reason": "主库不存在或不可读", "tiers": {}}
    try:
        where, migrated = _no_evidence_where(c)
        total = int(c.execute(f"SELECT COUNT(*) FROM knowledge WHERE {where}").fetchone()[0])
        split = c.execute(
            f"SELECT "
            # 与 _tier() 同优先级：有 http 来源的归 url_recheck，不再同时计入 local_quote
            f" SUM(CASE WHEN COALESCE(book_id,'')<>'' "
            f"          AND COALESCE(source_url,'') NOT LIKE 'http%' THEN 1 ELSE 0 END) local_quote,"
            f" SUM(CASE WHEN source_url LIKE 'http%' THEN 1 ELSE 0 END) url_recheck,"
            f" SUM(CASE WHEN COALESCE(book_id,'')='' "
            f"          AND COALESCE(source_url,'') NOT LIKE 'http%' THEN 1 ELSE 0 END) unresolvable"
            f" FROM knowledge WHERE {where}"
        ).fetchone()
        by_kind = {str(r[0] or ""): int(r[1]) for r in c.execute(
            f"SELECT source_kind, COUNT(*) FROM knowledge WHERE {where} GROUP BY source_kind"
        )}
        distinct_urls = int(c.execute(
            f"SELECT COUNT(DISTINCT source_url) FROM knowledge "
            f"WHERE {where} AND source_url LIKE 'http%'"
        ).fetchone()[0])
        with_ocr = 0
        try:
            with_ocr = int(c.execute(
                f"SELECT COUNT(*) FROM knowledge k WHERE {where} "
                "AND COALESCE(k.book_id,'')<>'' AND EXISTS("
                " SELECT 1 FROM ocr_pages o WHERE o.book_id=k.book_id "
                " AND COALESCE(o.text,'')<>'')"
            ).fetchone()[0])
        except Exception:  # noqa: BLE001  没有 ocr_pages 表的极简测试库
            pass
        samples: dict[str, list[dict]] = {t: [] for t in TIERS}
        for r in c.execute(
            f"SELECT id,topic,claim,source_url,source_kind,book_id FROM knowledge "
            f"WHERE {where} LIMIT 4000"
        ):
            row = dict(r)
            t = _tier(row)
            if len(samples[t]) < max(0, int(sample)):
                samples[t].append({"id": row["id"], "topic": row["topic"],
                                   "claim": str(row["claim"] or "")[:100],
                                   "source_url": str(row["source_url"] or "")[:120],
                                   "book_id": str(row["book_id"] or "")[:40]})
            if all(len(v) >= max(0, int(sample)) for v in samples.values()):
                break
        return {
            "available": True, "read_only": True, "schema_migrated": migrated,
            "no_evidence_total": total,
            "tiers": {
                "local_quote": {"count": int(split[0] or 0), "with_ocr_text": with_ocr,
                                "action": "本地 OCR 原文逐字补证（与联网无关）"},
                "url_recheck": {"count": int(split[1] or 0), "distinct_urls": distinct_urls,
                                "action": "来源 URL 可达性复检（去重后按域名限流）"},
                "unresolvable": {"count": int(split[2] or 0),
                                 "action": "无 book_id 无 URL，需重新采集或人工判定"},
            },
            "by_source_kind": by_kind,
            "samples": samples,
            "honest_note": (
                "98.8% 的待复核知识是本地书库导入，没有可回查的原网址；"
                "随机 300 条抽测仅 39 条能逐字命中 OCR 原文。"
                "「联网验证」对绝大多数条目不成立，本流水线按上述分层处理，"
                "命中引文也只标 legacy_literal，不自动标 verified。"
            ),
        }
    finally:
        c.close()


def _candidate_rows(c, limit: int | None = None) -> list[dict]:
    where, _ = _no_evidence_where(c)
    sql = (f"SELECT id,topic,claim,detail,source_url,source_kind,book_id,revision "
           f"FROM knowledge WHERE {where} ORDER BY id")
    if limit:
        sql += f" LIMIT {max(1, int(limit))}"
    try:
        return [dict(r) for r in c.execute(sql)]
    except Exception:  # noqa: BLE001  老库没有 revision 列
        sql = sql.replace(",revision ", " ")
        return [dict(r) for r in c.execute(sql)]


def enqueue(*, dry_run: bool = True, limit: int | None = None) -> dict:
    """把待复核项落进 review_queue。默认只报数，不写库。"""
    from . import memory
    with memory._lock:
        c = memory.conn()
        rows = _candidate_rows(c, limit)
        counts = {t: 0 for t in TIERS}
        for row in rows:
            counts[_tier(row)] += 1
        out: dict[str, Any] = {"ok": True, "dry_run": bool(dry_run),
                               "scanned": len(rows), "tiers": counts}
        if dry_run:
            return out
        now = time.time()
        with c:
            c.executemany(
                "INSERT OR IGNORE INTO review_queue(pid,tier,book_id,url,reason,state,enqueued_at) "
                "VALUES(?,?,?,?,?,'pending',?)",
                [(row["id"], _tier(row), row.get("book_id") or "",
                  row.get("source_url") or "", "无引文历史知识", now) for row in rows],
            )
            out["queued"] = int(c.execute(
                "SELECT COUNT(*) FROM review_queue WHERE state='pending'").fetchone()[0])
        return out


def _find_quote(c, book_id: str, claim: str, detail: str,
                max_pages: int = _DEFAULT_PAGES) -> tuple[str, dict] | None:
    """在该书的 OCR 原文里找一段能被逐字校验的引文。

    只认字面命中；改写、翻译、模型补充一律返回 None。命中后返回的是
    **原文里的片段**（不是 claim 的改写），因此以后再用
    evidence_supported(evidence, source_text) 复核仍然成立。
    """
    try:
        pages = c.execute(
            "SELECT id,page_no,text FROM ocr_pages WHERE book_id=? "
            "AND COALESCE(text,'')<>'' ORDER BY page_no LIMIT ?",
            (book_id, max(1, int(max_pages))),
        ).fetchall()
    except Exception:  # noqa: BLE001  没有 OCR 表
        return None
    folded = re.sub(r"\s+", " ", f"{claim or ''} {detail or ''}").strip()
    if len(folded) < _QUOTE_MIN:
        return None
    windows = []
    for size in (24, 16):
        if len(folded) >= size:
            windows.extend([folded[:size], folded[-size:],
                            folded[len(folded) // 2:len(folded) // 2 + size]])
    for p in pages:
        text = str(p["text"] or "")
        for w in dict.fromkeys(w for w in windows if len(w) >= _QUOTE_MIN):
            if evidence.evidence_supported(w, text):
                return w, {"id": p["id"], "page_no": p["page_no"]}
    return None


def run_local(*, batch: int = 50, dry_run: bool = True,
              max_pages: int = _DEFAULT_PAGES) -> dict:
    """本地逐字补证。命中只补 evidence/locator/hash，绝不改 claim 或状态。"""
    from . import governance, memory
    with memory._lock:
        c = memory.conn()
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM review_queue WHERE tier='local_quote' AND state='pending' "
            "ORDER BY enqueued_at LIMIT ?", (max(1, min(int(batch), 2000)),))]
        out: dict[str, Any] = {"ok": True, "dry_run": bool(dry_run),
                               "examined": len(rows), "cited": 0, "no_literal_match": 0}
        if not rows or dry_run:
            return out
        now = time.time()
        with c:
            for q in rows:
                row = c.execute("SELECT * FROM knowledge WHERE id=?", (q["pid"],)).fetchone()
                if row is None:
                    continue
                r = dict(row)
                hit = _find_quote(c, q["book_id"], r.get("claim") or "",
                                  r.get("detail") or "", max_pages=max_pages)
                if not hit:
                    c.execute("UPDATE review_queue SET state='no_literal_match',"
                              "note='OCR 原文中找不到逐字引文',reviewed_at=? WHERE pid=?",
                              (now, q["pid"]))
                    out["no_literal_match"] += 1
                    continue
                quote, page = hit
                cur = c.execute(
                    "UPDATE knowledge SET evidence=?,evidence_locator=?,evidence_hash=?,"
                    "verification='legacy_literal',updated_at=?,reviewed_at=? "
                    "WHERE id=? AND COALESCE(evidence,'')=''",
                    (quote, f"ocr_pages:{page['id']}#page={page['page_no']}",
                     evidence.sha256(quote), now, now, q["pid"]),
                )
                if cur.rowcount == 0:
                    # 入队后已被别的通路补上引文：不写修订快照（审计日志不能记录
                    # 没发生的改动），也不计入 cited。
                    c.execute("UPDATE review_queue SET state='cited',note='已有引文，未改写',"
                              "reviewed_at=? WHERE pid=?", (now, q["pid"]))
                    out["already_cited"] = out.get("already_cited", 0) + 1
                    continue
                governance._snapshot(c, q["pid"], r, "evidence_backfill",
                                     actor="review_pipeline",
                                     reason="本地 OCR 逐字补证（未做事实核查）")
                c.execute("UPDATE review_queue SET state='cited',note=?,reviewed_at=? "
                          "WHERE pid=?", (f"page={page['page_no']}", now, q["pid"]))
                out["cited"] += 1
        governance._finish(memory)
        return out


def _probe(url: str, timeout: float) -> tuple[str, str]:
    """返回 (status, note)。只做 HEAD，不下载正文，不解析内容。

    必须走共享客户端：urllib 默认 opener 会自动跟随 302 且接受
    file:/data:/ftp:，是 backend 里唯一绕开 SSRF request hook 的出站口。
    hook 在每一跳调用 assert_public_url，被拒时 ValueError 原样抛出。
    """
    from . import httpc

    try:
        httpc.assert_url_shape(url)
    except ValueError as e:
        return "dead", f"URL 结构非法：{e}"
    try:
        with httpc.borrow_client("fetch", timeout=timeout) as c:
            resp = c.head(url, timeout=timeout)
        code = int(resp.status_code)
        return ("ok" if 200 <= code < 400 else "changed"), f"HTTP {code}"
    except ValueError as e:
        return "dead", f"拒绝访问：{e}"
    except Exception as exc:  # noqa: BLE001  超时/不可达都记 dead，不抛给批量任务
        return "dead", f"{type(exc).__name__}: {str(exc)[:120]}"


def run_urls(*, limit: int | None = None, dry_run: bool = True,
             timeout: float = 6.0) -> dict:
    """对去重后的 http 来源做可达性复检。默认不发任何网络请求。

    memory._lock 是全局写锁：只在取 URL 快照与最终批量 UPDATE 时短暂持有，
    网络探测与 governance.source_check（自带同一把锁）必须在锁外执行。
    """
    from . import governance, memory
    with memory._lock:
        c = memory.conn()
        sql = ("SELECT DISTINCT url FROM review_queue WHERE tier='url_recheck' "
               "AND state='pending' AND url<>''")
        urls = [str(r[0]) for r in c.execute(sql)]
    if limit:
        urls = urls[:max(1, int(limit))]
    out: dict[str, Any] = {"ok": True, "dry_run": bool(dry_run),
                           "distinct_urls": len(urls), "checked": 0,
                           "statuses": {}}
    if dry_run:
        out["sample"] = urls[:5]
        return out
    for url in urls:
        status, note = _probe(url, timeout)
        governance.source_check(url, status, note=note)
        out["statuses"][status] = out["statuses"].get(status, 0) + 1
        out["checked"] += 1
    now = time.time()
    with memory._lock:
        with memory.conn() as c2:
            c2.executemany(
                "UPDATE review_queue SET state='url_checked',note=?,reviewed_at=? "
                "WHERE tier='url_recheck' AND url=?",
                [("来源可达性已复检", now, u) for u in urls],
            )
    return out


def status() -> dict:
    """队列进度 + 剩余无引文数，供界面与验收核对。"""
    from . import memory
    c = memory.conn()
    q: dict[str, dict[str, int]] = {}
    try:
        for r in c.execute("SELECT tier,state,COUNT(*) n FROM review_queue GROUP BY tier,state"):
            q.setdefault(r["tier"], {})[r["state"]] = int(r["n"])
    except Exception:  # noqa: BLE001  表未建
        return {"available": False, "queue": {}}
    where, migrated = _no_evidence_where(c)
    remaining = int(c.execute(f"SELECT COUNT(*) FROM knowledge WHERE {where}").fetchone()[0])
    total = int(c.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0])
    return {
        "available": True, "queue": q, "schema_migrated": migrated,
        "knowledge_total": total, "no_evidence_remaining": remaining,
        "evidence_coverage": round((total - remaining) / total, 4) if total else 0.0,
    }
