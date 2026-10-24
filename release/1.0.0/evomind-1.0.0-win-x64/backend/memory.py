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
CREATE TABLE IF NOT EXISTS cleanup_runs (
    id TEXT PRIMARY KEY, created_at REAL, removed INTEGER, restored_at REAL
);
CREATE TABLE IF NOT EXISTS cleanup_archive (
    run_id TEXT NOT NULL, point_id TEXT NOT NULL, kept_id TEXT NOT NULL,
    row_json TEXT NOT NULL, embedding BLOB, PRIMARY KEY(run_id,point_id)
);
CREATE INDEX IF NOT EXISTS idx_cleanup_point ON cleanup_archive(point_id);
CREATE INDEX IF NOT EXISTS idx_cleanup_kept ON cleanup_archive(kept_id);
CREATE TABLE IF NOT EXISTS knowledge_provenance (
    id TEXT PRIMARY KEY, point_id TEXT NOT NULL, row_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provenance_point ON knowledge_provenance(point_id);
CREATE TABLE IF NOT EXISTS refinements (
    id TEXT PRIMARY KEY, topic TEXT NOT NULL, items TEXT NOT NULL,
    sources TEXT NOT NULL, input_ids TEXT NOT NULL, input_count INTEGER,
    before_chars INTEGER, after_chars INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS refinement_coverage (
    point_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, refinement_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refinement_books (
    refinement_id TEXT PRIMARY KEY, book_id TEXT NOT NULL, book_title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_documents (
    url TEXT NOT NULL, topic TEXT NOT NULL, learned_at REAL,
    PRIMARY KEY (url, topic)
);
CREATE TABLE IF NOT EXISTS learning_queries (
    topic TEXT NOT NULL, query TEXT NOT NULL, used_at REAL,
    PRIMARY KEY (topic, query)
);
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

CREATE TABLE IF NOT EXISTS curriculum_runs (
    id TEXT PRIMARY KEY,
    seed TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    rounds_per_direction INTEGER DEFAULT 100,
    total_limit INTEGER DEFAULT 300,
    subdirection_count INTEGER DEFAULT 6,
    total_rounds INTEGER DEFAULT 0,
    started_at REAL,
    ended_at REAL,
    stop_reason TEXT DEFAULT '',
    summary TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS curriculum_directions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    seq INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    rounds INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    first_round INTEGER DEFAULT 0,
    last_round INTEGER DEFAULT 0,
    added_at REAL,
    note TEXT DEFAULT '',
    sessions TEXT DEFAULT '[]',
    session_total INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cd_run ON curriculum_directions(run_id);

CREATE TABLE IF NOT EXISTS curriculum_switches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL,
    from_direction TEXT DEFAULT '',
    to_direction TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    at_round INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cs_run ON curriculum_switches(run_id);

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
    for sql in (
        "ALTER TABLE knowledge ADD COLUMN book_id TEXT DEFAULT ''",
        # 方向轮换每批学完记一次会话 id，方便从汇总跳回具体学习记录
        "ALTER TABLE curriculum_directions ADD COLUMN sessions TEXT DEFAULT '[]'",
        # 会话明细只留最近 20 条，总数单独记，避免截断后统计偏小
        "ALTER TABLE curriculum_directions ADD COLUMN session_total INTEGER DEFAULT 0",
    ):
        try:
            c.execute(sql)
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
def doc_seen(url: str, topic: str | None = None) -> bool:
    # A readable page is not necessarily learned. Scope completion to the topic.
    sql = "SELECT 1 FROM learned_documents WHERE url=?"
    args = [url]
    if topic is not None:
        sql += " AND topic=?"
        args.append(topic)
    return bool(conn().execute(sql, args).fetchone())


def mark_learned(url: str, topic: str) -> None:
    c = conn()
    with c:
        c.execute("INSERT OR REPLACE INTO learned_documents VALUES (?,?,?)", (url, topic, time.time()))


def recent_queries(topic: str) -> list[str]:
    return [r[0] for r in conn().execute(
        "SELECT query FROM learning_queries WHERE topic=? ORDER BY used_at DESC LIMIT 24", (topic,)
    )]


def recent_gaps(topic: str) -> list[str]:
    row = conn().execute("SELECT note FROM sessions WHERE direction=? AND note!='' ORDER BY started_at DESC LIMIT 1", (topic,)).fetchone()
    try:
        items = json.loads(row[0]) if row else []
        return [s for s in items if isinstance(s, str)][:8] if isinstance(items, list) else []
    except (TypeError, ValueError):
        return []


def remember_queries(topic: str, queries: list[str]) -> None:
    c = conn()
    with c:
        c.executemany("INSERT OR REPLACE INTO learning_queries VALUES (?,?,?)",
                      [(topic, q, time.time()) for q in queries])


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
    _bump_topic_index()
    # 近重复扫描按库版本缓存；测试换库时版本号会归零，必须显式清掉
    _dup_cache.update(ver=-1, thr=None, topic=None, val=None)
    # 主题索引 / 向量缓存失效时，stats 的 SQL 部分也已与库不一致，一并作废
    _STATS_CACHE["db"] = None


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


# --------------------------------------------------------------------------
# 主题 → rowid 索引（加速 search(topic=...) 的过滤，去掉每调用一次的 DB 查询）
# --------------------------------------------------------------------------
# 原来的 search(topic=...) 每次都打 `SELECT rowid FROM knowledge WHERE topic=?`，
# 再 `mat[indices]` 把整块向量矩阵复制一遍（又违背了第三轮「矩阵给 view 不复制」
# 的初衷）。仪表盘轮询、分析视图、按方向问答都会高频触发主题过滤检索，
# 这些 DB 往返 + 矩阵复制纯属重复开销。
#
# 这里把「主题 → 该主题下 rowid 集合」常驻内存，写路径变更时整体失效（懒重建）。
# 过滤时直接拿内存里的集合生成布尔掩码，相似度在「整块 view」上算、用掩码
# 排除非本主题行 —— 零额外 DB 往返、零矩阵复制。
_topic_index: dict[str, set[int]] | None = None
_topic_index_lock = threading.Lock()
#: near_duplicates() 的版本键缓存：扫描是 O(Σ 主题内点数²)，库不变就不必重算。
_dup_cache: dict = {"ver": -1, "thr": None, "topic": None, "val": None}
# 压测/对照用：置 True 时 search(topic=) 退回「每次 DB 查询 + 矩阵复制」的旧路径
_DISABLE_TOPIC_INDEX = False


def _topic_index_get(topic: str) -> set[int]:
    """返回该主题下的 rowid 集合（内存索引，懒构建、写后失效）。

    键用库里存的主题原值，查找也用调用方传入的原值，与旧实现
    `WHERE topic=?` 的匹配语义逐行一致，保证检索结果零偏差。
    """
    global _topic_index
    with _topic_index_lock:
        if _topic_index is None:
            _topic_index = {}
            for r in conn().execute("SELECT rowid, topic FROM knowledge"):
                _topic_index.setdefault(r["topic"], set()).add(r["rowid"])
        return _topic_index.get(topic, set())


def _topic_index_all() -> dict[str, set[int]]:
    """整份主题索引（近重复扫描要按主题逐个比，需要拿到全部主题名）。"""
    global _topic_index
    with _topic_index_lock:
        if _topic_index is None:
            _topic_index = {}
            for r in conn().execute("SELECT rowid, topic FROM knowledge"):
                _topic_index.setdefault(r["topic"], set()).add(r["rowid"])
        return _topic_index


def _bump_topic_index() -> None:
    """知识库变更后让主题索引失效（rowid 集合已与库不一致）。"""
    global _topic_index
    with _topic_index_lock:
        _topic_index = None


def similarity(
    vec: list[float], rowids: list[int], mat: np.ndarray, *,
    k: int = 20, normalized: bool = False, allowed_mask: np.ndarray | None = None,
) -> list[tuple[int, float]]:
    if not rowids or k <= 0:
        return []
    q = _normalize(np.asarray([vec], dtype=np.float32))
    if q.shape[1] != mat.shape[1] or not np.isfinite(q).all() or not np.any(q):
        return []
    sims = ((mat if normalized else _normalize(mat)) @ q.T).ravel()
    if allowed_mask is not None:
        allowed_count = int(allowed_mask.sum())
        if allowed_count == 0:
            return []
        # 非本主题行压到 -1e9，使其永远排不进 top-k；窗口上限取 k 与「允许行数」的较小值，
        # 否则当 top-k 里混着大量非主题行时会被掩码剔掉、结果不足 k 条。
        sims = sims.copy()
        sims[~allowed_mask] = -1e9
        count = min(k, allowed_count)
    else:
        count = min(k, len(rowids))
    # 只对命中的 k 条排序，避免每次给整个知识库排序。
    candidates = np.argpartition(-sims, count - 1)[:count]
    order = candidates[np.argsort(-sims[candidates], kind="stable")]
    out: list[tuple[int, float]] = []
    for i in order:
        # 防御性兜底：理论上 -1e9 已保证 top-k 全是允许行，这里不再剔除。
        if allowed_mask is not None and not allowed_mask[i]:
            continue
        out.append((rowids[i], float(sims[i])))
        if len(out) >= k:
            break
    return out


# --------------------------------------------------------------------------
# 检索结果缓存 + 性能计数（加速仪表盘/分析视图的轮询与重复检索）
# --------------------------------------------------------------------------
# 仪表盘每几秒轮询一次、分析视图反复打开、同一方向反复问，都会触发大量
# 「库没变、查询也一样」的重复检索。原实现每次都把整份向量矩阵乘一遍
# （N×D @ D×1），库越大越亏。这里把结果按 (query, topic, k) 缓存，命中直接返回；
# 知识库一旦变更就整体失效，保证结果永远和「冷算」一致。
_search_cache: dict[tuple, list[dict]] = {}
_search_cache_lock = threading.Lock()

_perf = {
    "search_calls": 0,
    "search_hits": 0,
    "search_ms": 0.0,
    "snapshot_calls": 0,
    "snapshot_ms": 0.0,
    "status_calls": 0,
    "status_ms": 0.0,
}
_perf_lock = threading.Lock()
#: 最近若干次 /api/status 的耗时，供分析视图的「系统状态」卡片画折线
_status_series: list[float] = []
_STATUS_SERIES_MAX = 40


def record_status_poll(ms: float) -> None:
    """记录一次 /api/status 的端到端耗时（毫秒）。"""
    with _perf_lock:
        _perf["status_calls"] += 1
        _perf["status_ms"] += ms
        _status_series.append(round(ms, 2))
        if len(_status_series) > _STATUS_SERIES_MAX:
            del _status_series[: len(_status_series) - _STATUS_SERIES_MAX]


def _bump_search_cache() -> None:
    """知识库变更后让检索结果缓存整体失效（结果已和库不一致）。"""
    with _search_cache_lock:
        _search_cache.clear()


# --------------------------------------------------------------------------
# 写入版本号：知识库每发生一次写入就 +1
# --------------------------------------------------------------------------
# 分析聚合（snapshot / topic_graph）原来靠 3 秒 TTL 保鲜，而分析视图 6 秒才轮询一次，
# TTL 永远等不到命中——每次轮询都全量重算（2000 条时约 12ms，随库线性增长）。
# 这些聚合都是「库内容的确定函数」：同样的库状态算出来必然一样。所以正确的保鲜
# 方式不是时间，而是「库变没过」：一个单调递增的写入版本号，写路径 bump，
# 聚合按版本号缓存——没写过就永远不必重算。
#
# 安全网：分析侧仍保留 30s TTL。万一将来的新写入路径漏了 bump，最多陈旧 30 秒
# 后自愈，不会像纯版本键那样永久停在旧数据上。
_ver_lock = threading.Lock()
_ver = 0


def _bump_version() -> None:
    global _ver
    with _ver_lock:
        _ver += 1


def version() -> int:
    """当前知识库写入版本号（只增不减；进程重启后从 0 重新计，缓存随进程一起清空）。"""
    with _ver_lock:
        return _ver


def _search_cache_get(query: str, topic: str | None, k: int) -> list[dict] | None:
    return _search_cache.get((query, topic, k))


def _search_cache_put(query: str, topic: str | None, k: int, value: list[dict]) -> None:
    with _search_cache_lock:
        if len(_search_cache) > 2000:  # 防止内存无限增长
            _search_cache.clear()
        _search_cache[(query, topic, k)] = value


def perf_stats() -> dict:
    with _perf_lock:
        calls = _perf["search_calls"]
        hits = _perf["search_hits"]
        sms = _perf["search_ms"]
        sc = _perf["snapshot_calls"]
        sm = _perf["snapshot_ms"]
        tc = _perf["status_calls"]
        tm = _perf["status_ms"]
        series = list(_status_series)
    return {
        "search_calls": calls,
        "search_cache_hits": hits,
        "search_cache_hit_rate": round(hits / calls, 4) if calls else 0.0,
        "search_avg_ms": round(sms / calls, 3) if calls else 0.0,
        "snapshot_calls": sc,
        "snapshot_avg_ms": round(sm / sc, 3) if sc else 0.0,
        "status_calls": tc,
        "status_avg_ms": round(tm / tc, 3) if tc else 0.0,
        "status_series": series,
    }


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

    result = add_points_batch([dict(topic=topic, claim=claim, detail=detail, source_url=source_url,
        source_title=source_title, source_kind=source_kind, tags=list(tags), confidence=confidence, vec=vec)],
        session_id=session_id, book_id=book_id, dedup=dedup)
    return result.get("ids", [None])[0] if result["added"] else None


def add_points_batch(
    points: list[dict],
    *,
    session_id: str = "",
    book_id: str = "",
    dedup: bool = True,
) -> dict:
    """统一批量入库：文本身份 + 全库/批内余弦查重，完整度优先合并。

    一次计算候选向量和相似度矩阵；查重、内容更新、来源记录和插入在同一事务中。
    单条入口也调用这里，避免两个入口的去重与合并规则分叉。
    """
    valid: list[dict] = []
    need_embed: list[int] = []
    texts: list[str] = []
    for p in points:
        claim = (p.get("claim") or "").strip()
        if len(claim) < 8:
            continue
        vec = p.get("vec")
        if vec is None:
            detail = (p.get("detail") or "")[:400]
            need_embed.append(len(valid))
            texts.append(f"{claim} {detail}")
        else:
            arr = np.asarray(vec, dtype=np.float32)
            if arr.ndim != 1 or not arr.size or not np.isfinite(arr).all():
                continue
            if not np.isfinite(float(np.linalg.norm(arr))) or np.linalg.norm(arr) == 0:
                continue
        valid.append(p)

    if not valid:
        return {"added": 0, "dup": 0, "skipped": 0, "accepted": []}

    # 没带向量的，批量算一次（与 add_point 内部 emb[0] 实现一致，且已被验证与
    # 逐条结果逐位相同）
    emb = [None] * len(valid)
    for i, idx in enumerate(need_embed):
        if idx < len(valid):
            emb[idx] = None  # 占位，下面统一填充
    if need_embed:
        try:
            vecs = llm.embed(texts)
            for j, idx in enumerate(need_embed):
                emb[idx] = vecs[j] if j < len(vecs) else None
        except Exception:  # noqa: BLE001
            pass

    # 组装候选向量矩阵
    qvecs: list[np.ndarray] = []
    keep: list[dict] = []
    for i, p in enumerate(valid):
        v = emb[i] if emb[i] is not None else p.get("vec")
        if v is None:
            continue
        arr = np.asarray(v, dtype=np.float32)
        if (arr.ndim != 1 or not arr.size or not np.isfinite(arr).all()
                or not np.isfinite(np.linalg.norm(arr)) or np.linalg.norm(arr) == 0):
            continue
        qvecs.append(arr)
        keep.append(p)
    if not qvecs:
        return {"added": 0, "dup": 0, "skipped": len(valid), "accepted": []}

    from .knowledge_dedup import ingest
    result = ingest(keep, qvecs, session_id=session_id, book_id=book_id, dedup=dedup)
    result["skipped"] = len(valid) - len(keep)
    return result


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
    t0 = time.perf_counter()
    cacheable = vec is None  # 带了外部向量的查询不参与缓存（向量即身份，无法用 query 定位）
    if cacheable:
        hit = _search_cache_get(query, topic, k)
        if hit is not None:
            with _perf_lock:
                _perf["search_calls"] += 1
                _perf["search_hits"] += 1
            return hit
    if vec is None:
        vec = llm.embed([query])[0]
    rowids, mat = _all_vectors(len(vec))
    if not rowids:
        return []
    if topic:
        if _DISABLE_TOPIC_INDEX:
            # 旧路径：每次打 DB 查询 + 整块矩阵复制（对照用，压测时启用）
            allowed = {r[0] for r in conn().execute(
                "SELECT rowid FROM knowledge WHERE topic=?", (topic,))}
            indices = [i for i, rid in enumerate(rowids) if rid in allowed]
            rowids = [rowids[i] for i in indices]
            mat = mat[indices]
            if not rowids:
                return []
            hits = similarity(vec, rowids, mat, k=k, normalized=True)
        else:
            # 新路径：内存主题索引 + 布尔掩码，零 DB 往返、零矩阵复制
            allowed = _topic_index_get(topic)
            if not allowed:
                return []
            mask = np.array([rid in allowed for rid in rowids], dtype=bool)
            hits = similarity(vec, rowids, mat, k=k, normalized=True, allowed_mask=mask)
    else:
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
    out = out[:k]
    if cacheable:
        _search_cache_put(query, topic, k, out)
    with _perf_lock:
        _perf["search_calls"] += 1
        _perf["search_ms"] += (time.perf_counter() - t0) * 1000
    return out


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


def scale_warn(total: int) -> str:
    """规模提醒文案（单一真源：stats 与学习建议共用同一套阈值）。

    语义检索是全量向量比对，规模上去会线性变慢（1000 条约 25ms，1 万条约 250ms）。
    到阈值就明确提示，别等用户自己发现「怎么越来越卡」。
    """
    if total >= 20000:
        return (
            f"知识点已达 {total} 条，语义检索明显变慢（每次全量比对）。"
            "建议按方向拆分知识库，或引入近似索引（HNSW/FAISS）。"
        )
    if total >= 5000:
        return f"知识点已达 {total} 条，检索开始变慢，注意观察响应时间。"
    return ""


# --------------------------------------------------------------------------
# stats() 版本键缓存 + 磁盘 TTL
# --------------------------------------------------------------------------
# /api/status 是轮询最密的接口（总览每 6 秒一次），它每次都调 memory.stats()，
# 而 stats() 要跑 6 条 SQL（knowledge 总数 / 已读文档 / GROUP BY topic / 近 24h
# 新增 / 任务数 / 资源数）+ 两次磁盘调用。这 6 条全是「库内容 + 配置」的确定函数，
# 库没变过结果必然一样——旧实现每轮询都重跑一遍（2000 条约 2-4ms，随库线性增长），
# 每个打开总览的标签页每小时白烧几百次查询。
# 用写入版本号（memory.version，每次写入 +1）+ DB 文件身份做键缓存 SQL 部分；
# 磁盘占用变化以秒计，单独用 8s TTL（disk_usage_gb 内部已有 15s TTL，这里顺带
# 把 free_disk_gb 也一起收住，避免每次轮询都打一次 syscall）。
_STATS_CACHE: dict = {"key": None, "db": None, "disk": None, "disk_ts": 0.0}
_STATS_DISK_TTL = 8.0
#: 压测/对照用：置 True 时 stats() 退回「每次重跑 6 条 SQL」的旧路径
_DISABLE_STATS_CACHE = False


def _compute_stats_db() -> dict:
    """stats 里只依赖库内容的部分（6 条 SQL）。版本键缓存，库不变就跳过。"""
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
        "scale_warn": scale_warn(total),
    }


def stats() -> dict:
    global _STATS_CACHE
    ver = version()
    key = (ver, str(config.DB_FILE))
    db = None
    if not _DISABLE_STATS_CACHE and _STATS_CACHE["db"] is not None and _STATS_CACHE["key"] == key:
        db = _STATS_CACHE["db"]
    if db is None:
        db = _compute_stats_db()
        if not _DISABLE_STATS_CACHE:
            _STATS_CACHE["key"] = key
            _STATS_CACHE["db"] = db
    # 磁盘部分：8s TTL（占用是 GB 量级，8 秒内的变化肉眼不可见；下载前的预算闸门
    # 走 _disk_usage_gb_uncached，不受这里缓存影响）。
    now = time.time()
    disk = _STATS_CACHE["disk"]
    if disk is None or (now - _STATS_CACHE["disk_ts"]) >= _STATS_DISK_TTL:
        disk = (config.disk_usage_gb(), config.get("disk_budget_gb"), config.free_disk_gb())
        _STATS_CACHE["disk"] = disk
        _STATS_CACHE["disk_ts"] = now
    return {
        "points": db["points"],
        "documents": db["documents"],
        "new_24h": db["new_24h"],
        "sessions": db["sessions"],
        "resources": db["resources"],
        "topics": db["topics"],
        "scale_warn": db["scale_warn"],
        "disk_gb": disk[0],
        "budget_gb": disk[1],
        "free_disk_gb": disk[2],
        # 写入版本号：前端据此判断「库有没有变过」，没变就跳过整轮
        # 重新拉取 + 重渲染（知识库列表、分析图表都不必为同样的数据重建 DOM）。
        "ver": ver,
    }


def recent_claims(topics: list[str], per_topic: int = 3) -> dict[str, list[str]]:
    """每个主题最近 N 条结论，一次查询取回（方向汇总按方向列代表结论）。

    方向多的时候逐个 topic 查是 N 次往返；这里用窗口函数一次拿全。
    老版本 SQLite（<3.25，没有窗口函数）退回逐主题查询，结果语义一致。
    """
    topics = [t for t in dict.fromkeys(topics) if t]
    if not topics:
        return {}
    per_topic = max(1, min(int(per_topic), 20))
    out: dict[str, list[str]] = {}
    marks = ",".join("?" * len(topics))
    sql = (
        "SELECT topic, claim FROM ("
        " SELECT topic, claim,"
        " ROW_NUMBER() OVER (PARTITION BY topic ORDER BY created_at DESC) AS rn"
        f" FROM knowledge WHERE topic IN ({marks})"
        ") WHERE rn <= ?"
    )
    try:
        for r in conn().execute(sql, (*topics, per_topic)).fetchall():
            out.setdefault(r["topic"], []).append(r["claim"])
        # 没有命中的主题也要有条目：否则调用方得同时处理「键不存在」和
        # 「空列表」两种缺失形态，兜底分支和窗口函数分支的行为也会不一致。
        for topic in topics:
            out.setdefault(topic, [])
        return out
    except sqlite3.Error:
        pass
    for topic in topics:
        try:
            out[topic] = [p["claim"] for p in list_points(topic, limit=per_topic)]
        except Exception:
            out[topic] = []
    return out


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


def get_point(pid: str) -> dict | None:
    """按 id 取单条知识点全文（不含向量），供详情面板使用。"""
    row = conn().execute(
        "SELECT id,topic,claim,detail,source_url,source_title,source_kind,tags,"
        "confidence,created_at FROM knowledge WHERE id=?",
        (pid,),
    ).fetchone()
    from . import cleanup
    if not row:
        d = cleanup.archived_point(pid)
        if not d:
            return None
    else:
        d = dict(row)
    try:
        if not isinstance(d.get("tags"), list):
            d["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:
        d["tags"] = []
    d["duplicate_sources"] = cleanup.sources(pid)
    d["merged_sources"] = [json.loads(r[0]) for r in conn().execute(
        "SELECT row_json FROM knowledge_provenance WHERE point_id=?", (pid,))]
    return d


def similar_points(pid: str, k: int = 5) -> list[dict]:
    """与某条知识点语义最近的邻居（按向量余弦降序，排除自己）。

    直接复用已缓存、已归一化的向量矩阵：一条点的邻居就是它与全库的点积排序。
    点积在 numpy 里一次算完（2000 条约 0.1ms），不需要再过 LLM 做嵌入。
    找不到该点或无向量时返回空列表——调用方给「还没有足够数据」的引导。
    """
    k = max(0, min(int(k), 50))
    if k == 0:
        return []
    row = conn().execute(
        "SELECT rowid AS _rid, length(embedding)/4 dim FROM knowledge WHERE id=?", (pid,)
    ).fetchone()
    if not row or not row["dim"]:
        return []
    dim = int(row["dim"])
    rowids, mat = _all_vectors(dim)
    if not rowids:
        return []
    idx = {rid: i for i, rid in enumerate(rowids)}
    me = idx.get(row["_rid"])
    if me is None:  # 缓存里没有它（尚未建好/维度不一致）
        return []
    sims = mat @ mat[me]  # 全库对它的余弦（矩阵已是单位向量）
    # argpartition 只挑前 k+1 个（含自己），再对候选精确排序，O(n) 而非 O(n log n)
    want = min(len(rowids), k + 1)
    cand = np.argpartition(-sims, want - 1)[:want]
    cand = cand[np.argsort(-sims[cand])]
    picked = [(rowids[i], float(sims[i])) for i in cand if rowids[i] != row["_rid"]][:k]
    return _attach_similar_rows(picked)


def near_duplicates(threshold: float = .90, limit: int = 30, topic: str | None = None,
                    per_topic_cap: int = 400, offset: int = 0) -> dict:
    """Full-library representative pairs, matching the cleanup preview exactly."""
    from . import cleanup
    thr = max(.5, min(1., float(threshold)))
    limit, offset = max(1, min(200, int(limit))), max(0, int(offset))
    with _lock:
        ver = version()
        if (_dup_cache["val"] is None or _dup_cache["ver"] != ver or
                _dup_cache["thr"] != thr or _dup_cache["topic"] != topic):
            plan = cleanup.preview(thr, topic)
            _dup_cache.update(ver=ver, thr=thr, topic=topic, val=plan)
        plan = _dup_cache["val"]
        pairs = [{"sim": p["sim"], "a": p["keep"], "b": p["drop"]}
                 for p in plan["items"][offset:offset + limit]]
        return {"pairs": pairs, "scanned": plan["scanned"], "threshold": thr,
                "ver": ver, "capped": 0, "total": plan["removed"], "offset": offset, "invalid": plan["invalid"]}


def _attach_similar_rows(picked: list[tuple[int, float]]) -> list[dict]:
    """把 (rowid, 相似度) 补成完整知识点字典，保持相似度降序。"""
    if not picked:
        return []
    placeholders = ",".join("?" * len(picked))
    rows = conn().execute(
        f"SELECT rowid AS _rid, id,topic,claim,detail,source_url,source_title,"
        f"source_kind,tags,confidence,created_at FROM knowledge "
        f"WHERE rowid IN ({placeholders})",
        [rid for rid, _ in picked],
    ).fetchall()
    by_rid = {r["_rid"]: dict(r) for r in rows}
    out = []
    for rid, score in picked:
        rec = by_rid.get(rid)
        if not rec:
            continue
        rec.pop("_rid", None)
        try:
            rec["tags"] = json.loads(rec.get("tags") or "[]")
        except Exception:
            rec["tags"] = []
        rec["similarity"] = round(score, 4)
        out.append(rec)
    return out


def delete_point(pid: str) -> bool:
    with _lock:
        c = conn()
        with c:
            c.execute("DELETE FROM knowledge WHERE id=?", (pid,))
        _invalidate()
        _bump_search_cache()
        _bump_topic_index()
        _bump_version()
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


# --------------------------------------------------------------------------
# 方向轮换（学习方向自动更换）
# --------------------------------------------------------------------------
def curriculum_start_run(
    seed: str, rounds_per_direction: int, total_limit: int, subdirection_count: int
) -> str:
    rid = uuid.uuid4().hex[:12]
    c = conn()
    c.execute(
        "INSERT INTO curriculum_runs"
        "(id,seed,status,rounds_per_direction,total_limit,subdirection_count,started_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (rid, seed, "running", rounds_per_direction, total_limit, subdirection_count, time.time()),
    )
    c.commit()
    return rid


def curriculum_add_directions(run_id: str, items: list[dict]) -> None:
    """items: [{"id","direction","seq"}]，重复写入按主键忽略。

    run_id 为空说明调用方没先建任务；这类行永远查不回来（按 run_id 查），
    写进去只会变成孤儿数据，直接拒绝。
    """
    if not run_id or not items:
        return
    c = conn()
    with c:
        c.executemany(
            "INSERT OR IGNORE INTO curriculum_directions"
            "(id,run_id,direction,seq,status,rounds,points,first_round,last_round,added_at)"
            " VALUES (?,?,?,?,?,0,0,0,0,?)",
            [
                (it["id"], run_id, it["direction"], int(it.get("seq", 0)),
                 it.get("status", "pending"), time.time())
                for it in items
            ],
        )


def curriculum_update_direction(did: str, **kw) -> None:
    if not kw:
        return
    cols = ",".join(f"{k}=?" for k in kw)
    c = conn()
    c.execute(f"UPDATE curriculum_directions SET {cols} WHERE id=?", (*kw.values(), did))
    c.commit()


def curriculum_update_run(rid: str, **kw) -> None:
    if not kw:
        return
    cols = ",".join(f"{k}=?" for k in kw)
    c = conn()
    c.execute(f"UPDATE curriculum_runs SET {cols} WHERE id=?", (*kw.values(), rid))
    c.commit()


def curriculum_save_batch(did: str, rid: str, **fields) -> None:
    """把「方向进度 + 任务总轮次」放进同一个事务写。

    两条 UPDATE 各自提交时，进程在中间退出会留下「方向记了 5 轮、任务记了 0 轮」
    的错位进度；合成一个事务要么都成、要么都不成。未知字段忽略，避免拼错列名炸掉。
    """
    if not did or not rid:
        return
    allowed = ("rounds", "points", "last_round", "sessions", "session_total",
               "status", "first_round")
    dcols = {k: v for k, v in fields.items() if k in allowed}
    total = fields.get("total_rounds")
    c = conn()
    with c:
        if dcols:
            cols = ",".join(f"{k}=?" for k in dcols)
            c.execute(f"UPDATE curriculum_directions SET {cols} WHERE id=?", (*dcols.values(), did))
        if total is not None:
            c.execute("UPDATE curriculum_runs SET total_rounds=? WHERE id=?", (total, rid))


def curriculum_save_switch(rid: str, prev: dict | None, nxt: dict, switch: dict) -> None:
    """换向的三处写入放进同一个事务：关闭旧方向、激活新方向、记切换历史。

    分三次提交时，进程在中间退出会留下「切换历史写了、但没有任何方向是 active」
    的状态：恢复后界面显示在学某个方向，实际 current 是空的。
    """
    if not rid or not nxt.get("id"):
        return
    c = conn()
    with c:
        if prev and prev.get("id"):
            c.execute(
                "UPDATE curriculum_directions SET status=?, rounds=?, points=?, last_round=?"
                " WHERE id=?",
                (prev.get("status", "done"), int(prev.get("rounds") or 0),
                 int(prev.get("points") or 0), int(prev.get("last_round") or 0), prev["id"]),
            )
        c.execute(
            "UPDATE curriculum_directions SET status='active', first_round=? WHERE id=?",
            (int(nxt.get("first_round") or 0), nxt["id"]),
        )
        c.execute(
            "INSERT INTO curriculum_switches(run_id,ts,from_direction,to_direction,reason,at_round)"
            " VALUES (?,?,?,?,?,?)",
            (rid, time.time(), switch.get("from", ""), switch.get("to", ""),
             switch.get("reason", ""), int(switch.get("at_round") or 0)),
        )


def curriculum_log_switch(
    run_id: str, from_direction: str, to_direction: str, reason: str, at_round: int
) -> None:
    c = conn()
    c.execute(
        "INSERT INTO curriculum_switches(run_id,ts,from_direction,to_direction,reason,at_round)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, time.time(), from_direction, to_direction, reason, int(at_round)),
    )
    c.commit()


def curriculum_list_directions(run_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM curriculum_directions WHERE run_id=? ORDER BY seq, added_at", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def curriculum_list_switches(run_id: str, limit: int = 50) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM curriculum_switches WHERE run_id=? ORDER BY id DESC LIMIT ?",
        (run_id, max(1, min(limit, 500))),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def curriculum_get_run(rid: str) -> dict | None:
    row = conn().execute("SELECT * FROM curriculum_runs WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def curriculum_last_run() -> dict | None:
    row = conn().execute(
        "SELECT * FROM curriculum_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def curriculum_runs(limit: int = 20) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM curriculum_runs ORDER BY started_at DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [dict(r) for r in rows]


def curriculum_prune(keep: int = 100) -> int:
    """只保留最近 keep 次任务，连同子方向与切换记录一起清掉，返回删除的任务数。

    每次轮换都会写一条任务 + N 条子方向 + 若干切换记录；长期使用后这些表
    只增不减，界面上的「最近任务」又只看前 5 条，留着没有意义。
    """
    keep = max(1, min(int(keep), 1000))
    c = conn()
    rows = c.execute(
        "SELECT id FROM curriculum_runs ORDER BY started_at DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    stale = [r["id"] for r in rows]
    if not stale:
        return 0
    with c:
        for rid in stale:
            c.execute("DELETE FROM curriculum_directions WHERE run_id=?", (rid,))
            c.execute("DELETE FROM curriculum_switches WHERE run_id=?", (rid,))
            c.execute("DELETE FROM curriculum_runs WHERE id=?", (rid,))
    return len(stale)
