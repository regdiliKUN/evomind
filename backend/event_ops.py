"""A6 · 事件台账的分级保留与告警聚合。

原状：真实库 events 54,634 行（warn 20,267，占 37%），全库没有任何
``DELETE FROM events|sessions|learning_queries``，2 万条警告既无分类也无
阈值，等于日志黑洞；备份体积和启动扫描成本只增不减。

这里做三件事，全部**不丢数据**：

1. ``event_rollups`` 按 (天, kind) 记数，一条 UPSERT 就能回答「今天多少 warn」；
2. warn/error 累计到阈值时生成**一条** ``event_alerts`` 聚合告警，而不是让
   使用者自己在 2 万行里翻；
3. ``ops_archive`` 把过期的 events / learning_queries / sessions 以原始
   JSON 行搬走（不是 DELETE），主表行数因此有上界，历史仍可复查。

归档默认 ``dry_run=True``：任何 API 调用都只报数不动库；只有每日维护任务
显式传 ``dry_run=False`` 才真正搬。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from . import config

#: 同一天同一 kind 累计到这个数量时生成一条聚合告警。
ALERT_THRESHOLD = 20
#: 每次归档最多搬多少行，避免第一次启动把请求卡死。
ARCHIVE_BATCH = 5000
#: 主表保留天数。
DEFAULT_KEEP_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_rollups (
    day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER DEFAULT 0,
    first_ts REAL, last_ts REAL, sample TEXT DEFAULT '',
    PRIMARY KEY(day, kind)
);
CREATE TABLE IF NOT EXISTS event_alerts (
    id TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,
    n INTEGER DEFAULT 0, first_ts REAL, last_ts REAL,
    sample TEXT DEFAULT '', created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_day ON event_alerts(day, kind);
CREATE TABLE IF NOT EXISTS ops_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL, ts REAL, row_json TEXT NOT NULL, archived_at REAL
);
CREATE INDEX IF NOT EXISTS idx_archive_origin ON ops_archive(origin, ts);
"""


def ensure_schema(c: sqlite3.Connection) -> None:
    c.executescript(_SCHEMA)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


def note(c: sqlite3.Connection, kind: str, message: str, ts: float) -> dict | None:
    """在 log_event 的同一事务里记 rollup，跨阈值时产出一条聚合告警。

    返回新建的告警 dict（没跨阈值返回 None）。告警本身用直接 INSERT 写，
    不再走 log_event，避免自递归。
    """
    kind = (kind or "info").strip().lower()
    if kind == "alert":
        return None
    day = _day(ts)
    sample = str(message or "")[:240]
    c.execute(
        "INSERT INTO event_rollups(day,kind,n,first_ts,last_ts,sample) VALUES(?,?,1,?,?,?) "
        "ON CONFLICT(day,kind) DO UPDATE SET n=n+1, last_ts=excluded.last_ts, "
        "sample=CASE WHEN sample='' THEN excluded.sample ELSE sample END",
        (day, kind, ts, ts, sample),
    )
    if kind not in ("warn", "error"):
        return None
    row = c.execute("SELECT n,first_ts FROM event_rollups WHERE day=? AND kind=?",
                    (day, kind)).fetchone()

    def cell(r, name: str, idx: int, default: Any = 0) -> Any:
        if not r:
            return default
        try:
            return r[name]
        except (KeyError, IndexError, TypeError):
            try:
                return r[idx]
            except (KeyError, IndexError, TypeError):
                return default

    n = int(cell(row, "n", 0) or 0)
    try:
        threshold = max(1, int(config.get("event_alert_threshold", ALERT_THRESHOLD)))
    except Exception:  # noqa: BLE001
        threshold = ALERT_THRESHOLD
    if n < threshold or n % threshold != 0:
        return None
    existing = c.execute("SELECT id FROM event_alerts WHERE day=? AND kind=? AND n>=?",
                         (day, kind, n - threshold + 1)).fetchone()
    if existing:
        return None
    alert = {
        "id": f"alert-{day}-{kind}-{n}", "day": day, "kind": kind, "n": n,
        "first_ts": float(cell(row, "first_ts", 1, ts) or ts), "last_ts": ts,
        "sample": sample, "created_at": ts,
    }
    c.execute(
        "INSERT OR IGNORE INTO event_alerts(id,day,kind,n,first_ts,last_ts,sample,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (alert["id"], day, kind, n, alert["first_ts"], ts, sample, ts),
    )
    c.execute(
        "INSERT INTO events(ts,kind,message,payload) VALUES(?,?,?,?)",
        (ts, "alert", f"[{kind}] 当日累计 {n} 次，已聚合为一条告警：{sample[:80]}",
         json.dumps({"day": day, "kind": kind, "n": n}, ensure_ascii=False)),
    )
    return alert


def rollups(days: int = 7) -> dict:
    from . import memory
    days = max(1, min(int(days), 90))
    since = time.time() - days * 86400
    rows = memory.conn().execute(
        "SELECT day,kind,n,first_ts,last_ts,sample FROM event_rollups "
        "WHERE last_ts>=? ORDER BY day DESC,kind", (since,)).fetchall()
    by_day: dict[str, list[dict]] = {}
    total = 0
    for r in rows:
        d = dict(r)
        by_day.setdefault(d["day"], []).append(d)
        total += int(d["n"] or 0)
    return {"days": days, "total": total, "by_day": by_day,
            "threshold": int(config.get("event_alert_threshold", ALERT_THRESHOLD) or ALERT_THRESHOLD)}


def alerts(limit: int = 20) -> list[dict]:
    from . import memory
    return [dict(r) for r in memory.conn().execute(
        "SELECT * FROM event_alerts ORDER BY created_at DESC LIMIT ?",
        (max(1, min(int(limit), 200)),)).fetchall()]


def ops_status() -> dict:
    from . import memory
    c = memory.conn()
    def _n(sql: str) -> int:
        try:
            return int(c.execute(sql).fetchone()[0] or 0)
        except Exception:  # noqa: BLE001
            return 0
    return {
        "events": _n("SELECT COUNT(*) FROM events"),
        "rollup_rows": _n("SELECT COUNT(*) FROM event_rollups"),
        "alerts": _n("SELECT COUNT(*) FROM event_alerts"),
        "archived": _n("SELECT COUNT(*) FROM ops_archive"),
        "learning_queries": _n("SELECT COUNT(*) FROM learning_queries"),
        "sessions": _n("SELECT COUNT(*) FROM sessions"),
        "keep_days": int(config.get("event_keep_days", DEFAULT_KEEP_DAYS) or DEFAULT_KEEP_DAYS),
    }


def archive(*, c: sqlite3.Connection | None = None, dry_run: bool = True,
            keep_days: int | None = None, batch: int = ARCHIVE_BATCH) -> dict:
    """把过期的三类台账搬进 ops_archive（无损），主表行数因此有上界。

    ``dry_run=True``（默认）只统计不写。``batch`` 是**三张表合计**的上限，
    不是每表各一份：保证首次运行不会把 54,634 行一次性搬到请求路径上，
    剩余部分由每日维护分天消化。
    """
    own = c is None
    if own:
        from . import memory
        c = memory.conn()
    assert c is not None
    days = int(keep_days if keep_days is not None
               else config.get("event_keep_days", DEFAULT_KEEP_DAYS) or DEFAULT_KEEP_DAYS)
    days = max(1, days)
    cutoff = time.time() - days * 86400
    batch = max(1, min(int(batch), 50000))
    out: dict[str, Any] = {
        "ok": True, "dry_run": bool(dry_run), "keep_days": days,
        "cutoff": cutoff, "batch": batch, "moved": {},
    }
    specs = (
        ("events", "ts", "SELECT * FROM events WHERE ts < ? LIMIT ?"),
        ("learning_queries", "used_at",
         "SELECT * FROM learning_queries WHERE used_at < ? LIMIT ?"),
        ("sessions", "started_at",
         "SELECT * FROM sessions WHERE started_at < ? LIMIT ?"),
    )
    now = time.time()
    remaining = batch
    for origin, tscol, sql in specs:
        if remaining <= 0:
            out["moved"][origin] = 0
            continue
        try:
            rows = [dict(r) for r in c.execute(sql, (cutoff, remaining))]
        except sqlite3.OperationalError as exc:
            # 老库缺表/缺列时原本被当成「没有过期行」，ok 仍恒为 True：
            # 运维看到「归档成功 0 行」却无法区分表坏了。显式失败并留台账。
            out["ok"] = False
            out.setdefault("errors", {})[origin] = str(exc)[:200]
            out["moved"][origin] = 0
            try:
                from . import memory as _mem
                _mem.log_event("warn", f"归档读取失败：{origin} · {str(exc)[:120]}")
            except Exception:  # noqa: BLE001
                pass
            continue
        out["moved"][origin] = len(rows)
        remaining -= len(rows)
        if dry_run or not rows:
            continue
        # SAVEPOINT 而非裸两条 executemany：归档与删除必须同生共死。
        # 用嵌套事务而不是 `with c:`，own=False 时不会替调用方提前 commit。
        c.execute("SAVEPOINT evomind_archive")
        try:
            c.executemany(
                "INSERT INTO ops_archive(origin,ts,row_json,archived_at) VALUES(?,?,?,?)",
                [(origin, r.get(tscol), json.dumps(r, ensure_ascii=False, default=str), now)
                 for r in rows],
            )
            if origin == "events":
                c.executemany("DELETE FROM events WHERE id=?", [(r["id"],) for r in rows])
            elif origin == "learning_queries":
                c.executemany("DELETE FROM learning_queries WHERE topic=? AND query=?",
                              [(r["topic"], r["query"]) for r in rows])
            else:
                c.executemany("DELETE FROM sessions WHERE id=?", [(r["id"],) for r in rows])
            c.execute("RELEASE evomind_archive")
        except Exception:
            c.execute("ROLLBACK TO evomind_archive")
            c.execute("RELEASE evomind_archive")
            raise
    if own and not dry_run:
        c.commit()
    return out
