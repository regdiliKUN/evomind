"""知识库：SQLite 持久化 + 向量余弦检索 + 自动去重。

知识点（knowledge point）是最小学习单元：一句可独立成立的结论 + 出处。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

import numpy as np

from . import config, llm

_lock = threading.RLock()
_local = threading.local()
_vec_cache: tuple[list[int], np.ndarray] | None = None
_cache_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    claim TEXT NOT NULL,
    detail TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    source_title TEXT DEFAULT '',
    source_kind TEXT DEFAULT 'web',
    tags TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.6,
    embedding BLOB,
    session_id TEXT DEFAULT '',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_k_topic ON knowledge(topic);
CREATE INDEX IF NOT EXISTS idx_k_created ON knowledge(created_at);

CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    topic TEXT DEFAULT '',
    chars INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1,
    fetched_at REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    rounds INTEGER DEFAULT 0,
    new_points INTEGER DEFAULT 0,
    fetched INTEGER DEFAULT 0,
    started_at REAL,
    ended_at REAL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    rtype TEXT,
    repo TEXT,
    local_path TEXT DEFAULT '',
    size_gb REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    reason TEXT DEFAULT '',
    direction TEXT DEFAULT '',
    created_at REAL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    kind TEXT,
    message TEXT,
    payload TEXT DEFAULT '{}'
);
"""


def conn() -> sqlite3.Connection:
    """每个线程独立连接。SQLite 连接不能跨线程共享，否则并发写会事务冲突。"""
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(config.DB_FILE, check_same_thread=False, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=20000")
        c.executescript(SCHEMA)
        c.commit()
        _local.c = c
    return c


# --------------------------------------------------------------------------
# 事件日志
# --------------------------------------------------------------------------
def log_event(kind: str, message: str, payload: dict | None = None) -> None:
    try:
        c = conn()
        c.execute(
            "INSERT INTO events(ts, kind, message, payload) VALUES (?,?,?,?)",
            (time.time(), kind, message, json.dumps(payload or {}, ensure_ascii=False)),
        )
        c.commit()
    except Exception:
        pass


def recent_events(limit: int = 100) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            payload = json.loads(d.get("payload") or "{}")
        except Exception:
            payload = {}
        # 统一成前端事件格式（level/msg）
        out.append(
            {
                "id": d.get("id"),
                "ts": d.get("ts"),
                "level": d.get("kind") or "info",
                "msg": d.get("message") or "",
                "payload": payload,
            }
        )
    return out


# --------------------------------------------------------------------------
# 文档去重
# --------------------------------------------------------------------------
def doc_seen(url: str) -> bool:
    row = conn().execute("SELECT ok FROM documents WHERE url=?", (url,)).fetchone()
    return bool(row and row["ok"])


def mark_doc(url: str, title: str, topic: str, chars: int, ok: bool) -> None:
    c = conn()
    c.execute(
        "INSERT OR REPLACE INTO documents(url,title,topic,chars,ok,fetched_at) VALUES (?,?,?,?,?,?)",
        (url, title, topic, chars, 1 if ok else 0, time.time()),
    )
    c.commit()


# --------------------------------------------------------------------------
# 向量
# --------------------------------------------------------------------------
def _to_blob(vec: list[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _invalidate() -> None:
    global _vec_cache
    with _cache_lock:
        _vec_cache = None


def _all_vectors() -> tuple[list[int], np.ndarray]:
    global _vec_cache
    with _cache_lock:
        if _vec_cache is not None:
            return _vec_cache
        rows = conn().execute(
            "SELECT rowid, embedding FROM knowledge WHERE embedding IS NOT NULL"
        ).fetchall()
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for r in rows:
            try:
                v = _from_blob(r["embedding"])
                if v.size:
                    ids.append(r["rowid"])
                    vecs.append(v)
            except Exception:
                continue
        mat = np.vstack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)
        _vec_cache = (ids, mat)
        return _vec_cache


def _normalize(m: np.ndarray) -> np.ndarray:
    if m.size == 0:
        return m
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def similarity(vec: list[float], rowids: list[int], mat: np.ndarray) -> list[tuple[int, float]]:
    if not rowids:
        return []
    q = _normalize(np.asarray([vec], dtype=np.float32))
    sims = (_normalize(mat) @ q.T).ravel()
    order = np.argsort(-sims)[:20]
    return [(rowids[i], float(sims[i])) for i in order]


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
def add_point(
    *,
    topic: str,
    claim: str,
    detail: str = "",
    source_url: str = "",
    source_title: str = "",
    source_kind: str = "web",
    tags: Iterable[str] = (),
    confidence: float = 0.6,
    session_id: str = "",
    vec: list[float] | None = None,
    dedup: bool = True,
) -> str | None:
    """写入一个知识点。重复则返回 None。"""
    claim = claim.strip()
    if len(claim) < 8:
        return None
    if vec is None:
        vec = llm.embed([claim + " " + detail[:400]])[0]

    if dedup:
        rowids, mat = _all_vectors()
        for rid, score in similarity(vec, rowids, mat)[:3]:
            if score >= config.get("dedup_threshold", 0.93):
                return None

    pid = uuid.uuid4().hex[:16]
    c = conn()
    c.execute(
        "INSERT INTO knowledge(id,topic,claim,detail,source_url,source_title,source_kind,"
        "tags,confidence,embedding,session_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            pid,
            topic,
            claim,
            detail,
            source_url,
            source_title,
            source_kind,
            json.dumps(list(tags), ensure_ascii=False),
            confidence,
            _to_blob(vec),
            session_id,
            time.time(),
        ),
    )
    c.commit()
    _invalidate()
    return pid


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------
def search(
    query: str,
    *,
    k: int = 8,
    topic: str | None = None,
    vec: list[float] | None = None,
) -> list[dict]:
    if vec is None:
        vec = llm.embed([query])[0]
    rowids, mat = _all_vectors()
    if not rowids:
        return []
    hits = similarity(vec, rowids, mat)
    if not hits:
        return []
    top_ids = [rid for rid, _ in hits[: max(k * 4, 20)]]
    score_map = dict(hits)
    placeholders = ",".join("?" * len(top_ids))
    # 必须显式选出 rowid，SELECT * 不包含它
    sql = f"SELECT rowid AS _rid, * FROM knowledge WHERE rowid IN ({placeholders})"
    params: list[Any] = list(top_ids)
    if topic:
        sql += " AND topic=?"
        params.append(topic)
    rows = conn().execute(sql, params).fetchall()
    out = []
    for r in rows:
        rid = r["_rid"]
        d = dict(r)
        d.pop("_rid", None)
        d.pop("embedding", None)
        d["score"] = round(score_map.get(rid, 0.0), 4)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        out.append(d)
    out.sort(key=lambda x: -x["score"])
    return out[:k]


def keyword_search(query: str, k: int = 10) -> list[dict]:
    like = f"%{query}%"
    rows = conn().execute(
        "SELECT * FROM knowledge WHERE claim LIKE ? OR detail LIKE ? ORDER BY created_at DESC LIMIT ?",
        (like, like, k),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("embedding", None)
        d["score"] = 0.0
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        out.append(d)
    return out


# --------------------------------------------------------------------------
# 统计与列表
# --------------------------------------------------------------------------
def stats() -> dict:
    c = conn()
    total = c.execute("SELECT COUNT(*) n FROM knowledge").fetchone()["n"]
    docs = c.execute("SELECT COUNT(*) n FROM documents WHERE ok=1").fetchone()["n"]
    topics = c.execute(
        "SELECT topic, COUNT(*) n FROM knowledge GROUP BY topic ORDER BY n DESC LIMIT 30"
    ).fetchall()
    today = time.time() - 86400
    new24 = c.execute(
        "SELECT COUNT(*) n FROM knowledge WHERE created_at > ?", (today,)
    ).fetchone()["n"]
    sessions = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    res = c.execute("SELECT COUNT(*) n FROM resources WHERE status='done'").fetchone()["n"]
    return {
        "points": total,
        "documents": docs,
        "new_24h": new24,
        "sessions": sessions,
        "resources": res,
        "topics": [{"topic": r["topic"], "count": r["n"]} for r in topics],
        "disk_gb": config.disk_usage_gb(),
        "budget_gb": config.get("disk_budget_gb"),
        "free_disk_gb": config.free_disk_gb(),
    }


def list_points(topic: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    sql = "SELECT id,topic,claim,detail,source_url,source_title,source_kind,tags,confidence,created_at FROM knowledge"
    params: list[Any] = []
    if topic:
        sql += " WHERE topic=?"
        params.append(topic)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn().execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        out.append(d)
    return out


def delete_point(pid: str) -> bool:
    c = conn()
    c.execute("DELETE FROM knowledge WHERE id=?", (pid,))
    c.commit()
    _invalidate()
    return True


def export_training_pairs(topic: str | None = None, limit: int = 5000) -> list[dict]:
    """把知识点导出为「指令-回答」训练样本。"""
    sql = (
        "SELECT topic,claim,detail,source_title FROM knowledge "
        "WHERE LENGTH(claim) > 12"
    )
    params: list[Any] = []
    if topic:
        sql += " AND topic=?"
        params.append(topic)
    sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn().execute(sql, params).fetchall()
    pairs = []
    for r in rows:
        detail = (r["detail"] or "").strip()
        answer = r["claim"].strip()
        if detail and len(detail) > len(answer):
            answer = f"{answer}\n\n{detail}"
        pairs.append(
            {
                "instruction": f"请说明「{r['topic']}」领域中的这个要点：{r['claim'][:40]}",
                "input": "",
                "output": answer,
                "source": r["source_title"] or "",
            }
        )
    return pairs


# --------------------------------------------------------------------------
# 会话 / 资源
# --------------------------------------------------------------------------
def start_session(direction: str) -> str:
    sid = uuid.uuid4().hex[:12]
    c = conn()
    c.execute(
        "INSERT INTO sessions(id,direction,status,started_at) VALUES (?,?,?,?)",
        (sid, direction, "running", time.time()),
    )
    c.commit()
    return sid


def update_session(sid: str, **kw) -> None:
    if not kw:
        return
    cols = ",".join(f"{k}=?" for k in kw)
    c = conn()
    c.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*kw.values(), sid))
    c.commit()


def list_sessions(limit: int = 20) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_resource(
    rtype: str, repo: str, *, size_gb: float = 0, status: str = "pending",
    reason: str = "", direction: str = "", local_path: str = ""
) -> str:
    rid = uuid.uuid4().hex[:12]
    c = conn()
    c.execute(
        "INSERT INTO resources(id,rtype,repo,local_path,size_gb,status,reason,direction,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, rtype, repo, local_path, size_gb, status, reason, direction, time.time()),
    )
    c.commit()
    return rid


def update_resource(rid: str, **kw) -> None:
    if not kw:
        return
    cols = ",".join(f"{k}=?" for k in kw)
    c = conn()
    c.execute(f"UPDATE resources SET {cols} WHERE id=?", (*kw.values(), rid))
    c.commit()


def list_resources(limit: int = 100) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM resources ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def resource_exists(repo: str) -> bool:
    row = conn().execute(
        "SELECT 1 FROM resources WHERE repo=? AND status IN ('done','downloading')", (repo,)
    ).fetchone()
    return bool(row)
