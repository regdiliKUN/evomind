"""知识库：SQLite 持久化 + 向量余弦检索 + 自动去重。

知识点（knowledge point）是最小学习单元：一句可独立成立的结论 + 出处。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

import numpy as np

from . import config, llm

_lock = threading.RLock()
_local = threading.local()
_vec_cache: dict[int, "_VecStore"] | None = None
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
    book_id TEXT DEFAULT '',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_k_topic ON knowledge(topic);
CREATE INDEX IF NOT EXISTS idx_k_created ON knowledge(created_at);
CREATE INDEX IF NOT EXISTS idx_k_book ON knowledge(book_id);

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


def _upgrade(c: sqlite3.Connection) -> None:
    """给老库补列（CREATE TABLE IF NOT EXISTS 不会加列）。

    book_id 原本由 library.init_db() 用 ALTER 添加，但 knowledge 表现在由
    memory 自己写入，必须保证任何一条连接拿到的表都有这一列。
    索引不用在这里建：SCHEMA 里的 CREATE INDEX IF NOT EXISTS 每次建连接都会跑。
    """
    try:
        c.execute("ALTER TABLE knowledge ADD COLUMN book_id TEXT DEFAULT ''")
    except Exception:  # noqa: BLE001  列已存在
        pass


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
        _upgrade(c)
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


class _VecStore:
    """同一维度的 rowid + 单位向量，追加摊还 O(1)。

    直接用 np.vstack 追加会把整块矩阵复制一遍：4000 条 1024 维就是 16MB，
    导一本 240 点的书要复制几 GB。这里按 2 倍扩容预分配，写入位置永远在
    已有行之后，所以对外给出的 view 不会被后来的追加改写。
    """

    __slots__ = ("ids", "_buf", "n")

    def __init__(self, dim: int, cap: int = 0) -> None:
        self.ids: list[int] = []
        self._buf = np.zeros((max(cap, 1024), dim), dtype=np.float32)
        self.n = 0

    def add(self, rowid: int, unit: np.ndarray) -> None:
        if self.n == self._buf.shape[0]:
            grown = np.zeros((self._buf.shape[0] * 2, self._buf.shape[1]),
                             dtype=np.float32)
            grown[: self.n] = self._buf[: self.n]
            self._buf = grown
        self._buf[self.n] = unit
        self.ids.append(rowid)
        self.n += 1

    def view(self) -> np.ndarray:
        return self._buf[: self.n]


def _invalidate() -> None:
    global _vec_cache
    with _cache_lock:
        _vec_cache = None


def _append_vector(rowid: int, vec: list[float]) -> None:
    """把刚写入的向量追加进缓存，而不是把整份缓存作废。

    原来每写一条知识点就 _invalidate()，下一次读取要重扫整张 knowledge 表、
    重新做归一化与 vstack —— 写 N 条就是 N 次全表重建，总代价 O(N×语料量)。
    实测（1024 维、已有 4000 条）单次重建约 45ms，导入一本 240 点的书
    光重建就要 10 秒以上，而且越导越慢。

    追加写进缓存后的内容与「作废后重建」逐元素一致：同一批 rowid、同一批
    单位向量，只是免去重复计算。删除类操作仍然走 _invalidate()。
    """
    global _vec_cache
    with _cache_lock:
        if _vec_cache is None:
            return  # 缓存还没建过，下次 _all_vectors 自然会把它一起读进来
        dim = len(vec)
        v = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if not np.isfinite(norm) or norm == 0:
            return
        store = _vec_cache.get(dim)
        if store is None:
            store = _vec_cache[dim] = _VecStore(dim)
        store.add(rowid, v / norm)


def _all_vectors(dimension: int) -> tuple[list[int], np.ndarray]:
    """按维度缓存单位向量，避免模型切换后混合维度导致矩阵形状不匹配。"""
    global _vec_cache
    with _cache_lock:
        if _vec_cache is None:
            rows = conn().execute(
                "SELECT rowid, embedding FROM knowledge WHERE embedding IS NOT NULL"
            ).fetchall()
            groups: dict[int, tuple[list[int], list[np.ndarray]]] = {}
            for r in rows:
                try:
                    v = _from_blob(r["embedding"])
                    if not v.size or not np.isfinite(v).all():
                        continue
                    norm = float(np.linalg.norm(v))
                    if not np.isfinite(norm) or norm == 0:
                        continue
                    ids, vecs = groups.setdefault(v.size, ([], []))
                    ids.append(r["rowid"])
                    vecs.append(v / norm)
                except (TypeError, ValueError):
                    continue
            _vec_cache = {}
            for size, (ids, vecs) in groups.items():
                store = _VecStore(size, cap=len(ids))
                store.ids = ids
                store.n = len(ids)
                if vecs:
                    store._buf[: len(ids)] = np.asarray(vecs, dtype=np.float32)
                _vec_cache[size] = store
        store = _vec_cache.get(dimension)
        if store is None:
            return [], np.empty((0, dimension), dtype=np.float32)
        # 矩阵给 view 而不是副本，否则每次读取都要复制 16MB，优化就白做了；
        # 追加只写 n 之后的位置，view 不会被改写。
        # id 列表则给快照：它是活的，并发追加会让它比矩阵长，
        # similarity 里 k 超过矩阵行数就会下标越界。复制 4000 个指针只要几微秒。
        return store.ids[:], store.view()


def _normalize(m: np.ndarray) -> np.ndarray:
    if m.size == 0:
        return m
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def similarity(
    vec: list[float], rowids: list[int], mat: np.ndarray, *,
    k: int = 20, normalized: bool = False,
) -> list[tuple[int, float]]:
    if not rowids or k <= 0:
        return []
    q = _normalize(np.asarray([vec], dtype=np.float32))
    if q.shape[1] != mat.shape[1] or not np.isfinite(q).all() or not np.any(q):
        return []
    sims = ((mat if normalized else _normalize(mat)) @ q.T).ravel()
    count = min(k, len(rowids))
    # 只对命中的 k 条排序，避免每次给整个知识库排序。
    candidates = np.argpartition(-sims, count - 1)[:count]
    order = candidates[np.argsort(-sims[candidates], kind="stable")]
    return [(rowids[i], float(sims[i])) for i in order]


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
def _norm_topic(topic: str) -> str:
    """主题归一化：把换行、制表符和连续空格压成单个空格。

    学习方向常是多行粘贴进来的（配置里的 last_direction 就带着换行和缩进），
    同一个方向因此会存成两个不同的主题：界面上出现两条几乎一样的分组，
    检索也各算各的。主题只用于分组，压平不影响语义。
    """
    return " ".join(str(topic or "").split())


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
    book_id: str = "",
    vec: list[float] | None = None,
    dedup: bool = True,
) -> str | None:
    """写入一个知识点。重复则返回 None。

    book_id 直接随 INSERT 写入，省掉调用方「先插入再 UPDATE 回填」的
    一次多余 UPDATE + 一次 commit（导入一本 240 点的书就是 240 次）。
    """
    claim = claim.strip()
    topic = _norm_topic(topic)
    if len(claim) < 8:
        return None
    if vec is None:
        vec = llm.embed([claim + " " + detail[:400]])[0]

    vector = np.asarray(vec, dtype=np.float32)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError("知识点向量必须是非空的有限数值数组")
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("知识点向量的长度必须是有限正数")

    # 推理在锁外；检查、插入与缓存失效必须串行，防止并发重复入库。
    with _lock:
        if dedup:
            rowids, mat = _all_vectors(len(vec))
            for rid, score in similarity(vec, rowids, mat, k=1, normalized=True):
                if score >= config.get("dedup_threshold", 0.93):
                    return None

        pid = uuid.uuid4().hex[:16]
        c = conn()
        with c:
            c.execute(
                "INSERT INTO knowledge(id,topic,claim,detail,source_url,source_title,source_kind,"
                "tags,confidence,embedding,session_id,book_id,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, topic, claim, detail, source_url, source_title, source_kind,
                 json.dumps(list(tags), ensure_ascii=False), confidence,
                 _to_blob(vec), session_id, book_id, time.time()),
            )
            rowid = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        # 增量追加，而不是把整份缓存作废（见 _append_vector）
        _append_vector(rowid, vec)
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
    k = max(0, min(int(k), 1000))
    if not query.strip() or k == 0:
        return []
    if vec is None:
        vec = llm.embed([query])[0]
    rowids, mat = _all_vectors(len(vec))
    if not rowids:
        return []
    if topic:
        allowed = {r[0] for r in conn().execute("SELECT rowid FROM knowledge WHERE topic=?", (topic,))}
        indices = [i for i, rid in enumerate(rowids) if rid in allowed]
        rowids = [rowids[i] for i in indices]
        mat = mat[indices]
    hits = similarity(vec, rowids, mat, k=k, normalized=True)
    if not hits:
        return []
    top_ids = [rid for rid, _ in hits]
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
    k = max(0, min(int(k), 1000))
    if not query.strip() or k == 0:
        return []
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
def count_points(topic: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM knowledge"
    params = ()
    if topic:
        sql += " WHERE topic=?"
        params = (topic,)
    return conn().execute(sql, params).fetchone()[0]


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
    # 语义检索是全量向量比对，规模上去会线性变慢（1000 条约 25ms，1 万条约 250ms）。
    # 到阈值就明确提示，别等用户自己发现「怎么越来越卡」。
    scale_warn = ""
    if total >= 20000:
        scale_warn = (
            f"知识点已达 {total} 条，语义检索明显变慢（每次全量比对）。"
            "建议按方向拆分知识库，或引入近似索引（HNSW/FAISS）。"
        )
    elif total >= 5000:
        scale_warn = f"知识点已达 {total} 条，检索开始变慢，注意观察响应时间。"
    return {
        "points": total,
        "documents": docs,
        "new_24h": new24,
        "sessions": sessions,
        "resources": res,
        "scale_warn": scale_warn,
        "topics": [{"topic": r["topic"], "count": r["n"]} for r in topics],
        "disk_gb": config.disk_usage_gb(),
        "budget_gb": config.get("disk_budget_gb"),
        "free_disk_gb": config.free_disk_gb(),
    }


def list_points(topic: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    # 夹紧分页参数：SQLite 的 LIMIT -1 会返回全表
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

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
    with _lock:
        c = conn()
        with c:
            c.execute("DELETE FROM knowledge WHERE id=?", (pid,))
        _invalidate()
    return True


def _make_instruction(claim: str) -> str:
    """把一条陈述改写成能自然回答它的问句（规则版，作为 LLM 改写失败时的兜底）。

    关键：不能用 topic 拼问句。topic 常常是「书库 · 某本书名」这类无意义值，
    拼出来会变成「请说明「这本书」领域中的这个要点」，答案与问题完全对不上。
    所以一律从 claim 自身取第一个完整分句来构造，避免截断在词中间。
    """
    c = claim.strip().rstrip("。．.!！;；")
    parts = [x.strip() for x in re.split(r"[，,。；;：:！!？?]", c) if x.strip()]
    core = parts[0] if parts else c
    # 第一个分句常常是「在《X》设定中」这类介词短语，信息量不够，合并下一句
    if len(core) < 14 and len(parts) > 1:
        core = f"{parts[0]}，{parts[1]}"
    if len(core) > 40:
        core = core[:40].rstrip() + "…"
    if len(core) < 8:
        core = c[:40].rstrip()

    # 按陈述语气选问法，比统一模板更像真实提问
    if any(k in c for k in ("应该", "需要", "必须", "建议", "最好")):
        return f"{core}，原因是什么？"
    if any(k in c for k in ("指的是", "称为", "叫做", "属于", "是一种")):
        return f"{core}，具体指什么？"
    if any(k in c for k in ("可以", "能够", "用于", "适合", "用来")):
        return f"{core}，具体怎么用？"
    if any(k in c for k in ("因为", "导致", "造成", "源于")):
        return f"{core}，是什么原因？"
    return f"{core}，这是怎么回事？"


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
                "instruction": _make_instruction(answer),
                "input": "",
                "output": answer,
                "source": r["source_title"] or "",
                "topic": r["topic"] or "",
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
