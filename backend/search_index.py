"""A3 · 词法索引与混合召回（FTS5 / BM25 + RRF）。

设计约束（刻意不把索引塞进 256 MB 主库）：

* 索引放在独立的 ``data/knowledge/search_index.sqlite``。它是**纯派生数据**：
  删掉随时可重建，主库 schema 一行都不用改，也不给老库迁移增加新的启动风险。
* 中文没有空格分词，``unicode61`` 会把整句当一个词。用 FTS5 的
  ``trigram`` tokenizer（SQLite >= 3.34），它按 3 字滑窗建索引，能命中
  「切片软件 / ZBrush 4R6 / Cura」这类精确术语和数字型号。
* ``pid`` 存 knowledge.id，``rowid`` 与主库 rowid 对齐；额外的 ``fts_pid``
  映射表让增量同步不必对 UNINDEXED 列做全表扫描。
* 检索时用 ACTIVE_FILTER 回主库二次过滤，archived/disputed/superseded
  永远不会因为索引里还留着旧行而漏进结果。

对上层只暴露四个动作：rebuild / sync_many / bm25_search / hybrid_search。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config, evidence

#: RRF 融合常数。60 是文献里的常用取值，能把 rank 差异压到同一量级，
#: 避免某一路的原始分数（余弦 0~1 / bm25 任意负值）直接支配排序。
RRF_K = 60
_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]{2,}", re.UNICODE)
_lock = threading.Lock()
_available: bool | None = None

#: 索引库是独立存储，不要混进主库的连接生命周期。测试会 patch
#: ``sqlite3.connect`` 来记录/关闭所有主库连接；如果这里也走被 patch 的函数，
#: TestClient / 线程池里建的索引连接会在主线程被 close，抛
#: "SQLite objects created in a thread can only be used in that same thread"。
#: 注意不能写成 ``_RAW_CONNECT = sqlite3.connect``：本模块是懒加载的，首次
#: import 可能正好落在 patch 生效期间，那样捕获到的是 MagicMock（实测踩过）。
#: ``sqlite3.dbapi2.connect`` 与 ``sqlite3.connect`` 是同一个函数对象，但它
#: 位于子模块命名空间，patch 包属性不会影响它，因此与导入时机无关。
from sqlite3 import dbapi2 as _dbapi2

_RAW_CONNECT = _dbapi2.connect


def _index_path() -> Path:
    return Path(config.KNOWLEDGE) / "search_index.sqlite"


def _main_conn() -> sqlite3.Connection | None:
    """只读连主库，同样不碰 memory.conn() 的线程局部连接。"""
    try:
        p = Path(config.DB_FILE).resolve()
        if not p.exists():
            return None
        c = _RAW_CONNECT(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30.0)
        c.row_factory = sqlite3.Row
        return c
    except (sqlite3.Error, OSError):
        return None


def _connect() -> sqlite3.Connection | None:
    """打开索引库并确保表存在。FTS5 不可用时返回 None（自动降级，不抛错）。"""
    global _available
    p = _index_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        c = _RAW_CONNECT(str(p), timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING "
            "fts5(pid UNINDEXED, topic, claim, detail, tokenize='trigram')"
        )
        c.execute("CREATE TABLE IF NOT EXISTS fts_pid(pid TEXT PRIMARY KEY, rid INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.commit()
        _available = True
        return c
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
        _available = False
        try:
            c.close()  # noqa: F821  c 可能未绑定，下面统一兜住
        except Exception:  # noqa: BLE001
            pass
        return None


def available() -> bool:
    global _available
    if _available is None:
        c = _connect()
        if c is not None:
            c.close()
    return bool(_available)


def _rows_from_main(where: str = "", args: tuple = (), batch: int = 2000,
                    after_rid: int = 0) -> list[sqlite3.Row]:
    c = _main_conn()
    if c is None:
        return []
    try:
        return c.execute(
            "SELECT rowid AS rid, id, topic, claim, detail FROM knowledge "
            f"WHERE rowid > ? {where} ORDER BY rowid LIMIT ?",
            (after_rid, *args, batch),
        ).fetchall()
    finally:
        c.close()


def rebuild(*, batch: int = 2000, only_if_stale: bool = False) -> dict:
    """从主库全量重建词法索引。幂等，可随时调用；返回可核对的计数。"""
    t0 = time.perf_counter()
    with _lock:
        if only_if_stale:
            st = status()
            if not st.get("stale"):
                return {"available": True, "indexed": st.get("indexed", 0),
                        "rebuilt": False, "reason": "并发重建已完成",
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
        ic = _connect()
        if ic is None:
            return {"available": False, "reason": "FTS5/trigram 不可用", "indexed": 0}
        try:
            ic.execute("DELETE FROM knowledge_fts")
            ic.execute("DELETE FROM fts_pid")
            total = 0
            last = 0
            while True:
                rows = _rows_from_main(batch=batch, after_rid=last)
                if not rows:
                    break
                ic.executemany(
                    "INSERT INTO knowledge_fts(rowid,pid,topic,claim,detail) VALUES(?,?,?,?,?)",
                    [(r["rid"], r["id"], r["topic"] or "", r["claim"] or "", r["detail"] or "")
                     for r in rows],
                )
                ic.executemany(
                    "INSERT OR REPLACE INTO fts_pid(pid,rid) VALUES(?,?)",
                    [(r["id"], r["rid"]) for r in rows],
                )
                last = int(rows[-1]["rid"])
                total += len(rows)
            ic.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES('rows',?)", (str(total),))
            ic.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES('built_at',?)",
                       (str(time.time()),))
            ic.commit()
            # 派生向量索引与主库保持同步：ANN 开启时一并重建（失败不影响词法索引）。
            try:
                from . import vector_ann
                if vector_ann.enabled():
                    vector_ann.rebuild()
            except Exception:  # noqa: BLE001
                pass
            return {
                "available": True, "indexed": total, "rebuilt": True,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "tokenizer": "trigram", "path": str(_index_path()),
            }
        finally:
            ic.close()


def sync_many(ids: Iterable[str]) -> int:
    """增量同步指定知识点（新增、纠错改写、物理删除后调用）。"""
    ids = [str(x) for x in dict.fromkeys(ids or []) if x]
    if not ids or not available():
        return 0
    mc = _main_conn()
    if mc is None:
        return 0
    with _lock:
        ic = _connect()
        if ic is None:
            mc.close()
            return 0
        try:
            changed = 0
            for i in range(0, len(ids), 400):
                part = ids[i:i + 400]
                marks = ",".join("?" * len(part))
                rows = {r["id"]: r for r in mc.execute(
                    "SELECT rowid AS rid,id,topic,claim,detail FROM knowledge "
                    f"WHERE id IN ({marks})", part).fetchall()}
                for pid in part:
                    old = ic.execute("SELECT rid FROM fts_pid WHERE pid=?", (pid,)).fetchone()
                    if old:
                        ic.execute("DELETE FROM knowledge_fts WHERE rowid=?", (old["rid"],))
                        ic.execute("DELETE FROM fts_pid WHERE pid=?", (pid,))
                    row = rows.get(pid)
                    if row is None:
                        continue                       # 物理删除：只清索引
                    rid = int(row["rid"])
                    ic.execute(
                        "INSERT INTO knowledge_fts(rowid,pid,topic,claim,detail) VALUES(?,?,?,?,?)",
                        (rid, pid, row["topic"] or "", row["claim"] or "", row["detail"] or ""),
                    )
                    ic.execute("INSERT OR REPLACE INTO fts_pid(pid,rid) VALUES(?,?)", (pid, rid))
                    changed += 1
            ic.execute("INSERT OR REPLACE INTO index_meta(key,value) "
                       "VALUES('rows',(SELECT COUNT(*) FROM knowledge_fts))")
            ic.commit()
            return changed
        finally:
            ic.close()
            mc.close()


def status() -> dict:
    """索引新鲜度：indexed 与 knowledge_total 不一致即需要重建。"""
    ic = _connect()
    if ic is None:
        return {"available": False, "reason": "FTS5/trigram 不可用", "indexed": 0,
                "knowledge_total": 0, "stale": True}
    try:
        indexed = int(ic.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0])
        built = ic.execute("SELECT value FROM index_meta WHERE key='built_at'").fetchone()
        flag = ic.execute("SELECT value FROM index_meta WHERE key='rows'").fetchone()
        mc = _main_conn()
        total = 0
        if mc is not None:
            try:
                total = int(mc.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0])
            finally:
                mc.close()
        return {
            "available": True, "indexed": indexed, "knowledge_total": total,
            "stale": indexed != total or (flag is not None and flag[0] == "-1"),
            "built_at": float(built[0]) if built else None,
            "tokenizer": "trigram", "path": str(_index_path()),
        }
    finally:
        ic.close()


def mark_stale() -> None:
    """去重合并/批量改写后声明索引过期，下次查询会全量重建一次。

    合并会改写既有行的 claim/detail，而调用方拿不到被合并行的 id；与其
    漏掉几行让索引悄悄说谎，不如显式标记过期。
    """
    ic = _connect()
    if ic is None:
        return
    try:
        ic.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES('rows','-1')")
        ic.commit()
    finally:
        ic.close()


def ensure_ready(*, force: bool = False) -> bool:
    """首次使用或发现计数漂移时自动重建；正常路径只做两次 COUNT。"""
    if not available():
        return False
    if force:
        rebuild()
        return True
    st = status()
    if st.get("stale"):
        # 并发查询会同时看到 stale；交给 rebuild 持锁后二次复核，只做一次
        # 21k 行全量重建（也只连带触发一次 ANN 重建）。
        rebuild(only_if_stale=True)
    return True


def _fts_query(query: str) -> str:
    """把自然语言查询转成 trigram 可吃的 OR 短语串。

    直接把整句加引号会要求整句逐字命中，召回太窄；切成词后取前 8 个，
    长度 < 3 的片段丢掉（trigram 最小 3 字），保留精确术语与型号数字。
    """
    terms = [t for t in _TOKEN_RE.findall(query) if len(t) >= 3]
    if not terms:
        flat = re.sub(r"\s+", " ", query).strip().replace('"', "")
        terms = [flat] if len(flat) >= 3 else []
    terms = list(dict.fromkeys(terms))[:8]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _shape(row: dict, score: float, bm: float) -> dict:
    d = dict(row)
    d.pop("embedding", None)
    d.pop("_rid", None)
    d["score"] = round(score, 6)
    # trigram 分词下 bm25() 的原始量级常在 1e-6（实测 -2.7e-06）。用 4 位小数
    # 会把非零分数抹成 0.0，调用方看起来又变成「关键词检索没有相关度」——
    # 正是 A3 要修的旧症状。保留足够精度，score 另做结果集内归一化。
    d["bm25"] = round(bm, 10)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:  # noqa: BLE001
        d["tags"] = []
    return d


def bm25_search(query: str, k: int = 10, topic: str | None = None) -> list[dict]:
    """词法召回，返回带 BM25 相对分的结果；FTS5 不可用时返回空列表由调用方降级。"""
    k = max(0, min(int(k), 1000))
    if not query.strip() or k == 0 or not ensure_ready():
        return []
    match = _fts_query(query)
    if not match:
        return []
    ic = _connect()
    if ic is None:
        return []
    try:
        try:
            raw = ic.execute(
                "SELECT rowid AS rid,pid,bm25(knowledge_fts) AS bm FROM knowledge_fts "
                "WHERE knowledge_fts MATCH ? ORDER BY bm ASC LIMIT ?",
                (match, max(k * 5, 30)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if not raw:
            return []
        pids = [r["pid"] for r in raw]
        mc = _main_conn()
        if mc is None:
            return []
        out: list[dict] = []
        # bm25 是负数，越小越好；用结果集内最大值归一化到 (0,1]，保证 score 参与排序
        ceiling = max((-float(r["bm"]) for r in raw), default=0.0) or 1.0
        for i in range(0, len(pids), 400):
            part = pids[i:i + 400]
            marks = ",".join("?" * len(part))
            sql = (f"SELECT * FROM knowledge WHERE id IN ({marks}) "
                   f"AND {evidence.ACTIVE_FILTER}")
            args: list[Any] = list(part)
            if topic:
                sql += " AND topic=?"
                args.append(topic)
            found = {r["id"]: dict(r) for r in mc.execute(sql, args)}
            for r in raw[i:i + 400]:
                row = found.get(r["pid"])
                if row is None:
                    continue
                bm = -float(r["bm"])
                out.append(_shape(row, min(1.0, max(0.0001, bm / ceiling)), bm))
        out.sort(key=lambda x: -x["score"])
        return out[:k]
    finally:
        ic.close()
        if 'mc' in locals() and mc is not None:
            mc.close()


def _vector_hits(query: str, k: int, topic: str | None, vec: list[float] | None) -> list[dict]:
    """向量召回：ANN 开启且可用时走 FAISS HNSW，否则回退 memory.search 暴力余弦。

    关闭 ANN（默认）时本函数与改造前行为完全一致：调用 memory.search。
    ANN 任一环节抛错都静默回退，绝不因为「加速层」没就绪而让检索整体失败。
    """
    if config.get("vector_ann_enabled"):
        try:
            from . import vector_ann
            if vector_ann.available():
                hits = vector_ann.vector_hits(query, k, topic=topic, vec=vec)
                if hits is not None:
                    return hits
        except Exception:  # noqa: BLE001
            pass
    from . import memory
    try:
        return memory.search(query, k=k, topic=topic, vec=vec)
    except Exception:  # noqa: BLE001  embed 后端不可用时仍可用词法召回
        return []


def hybrid_search(
    query: str,
    k: int = 8,
    topic: str | None = None,
    *,
    alpha: float = 0.5,
    vec: list[float] | None = None,
    vector_hits: list[dict] | None = None,
    lexical_hits: list[dict] | None = None,
) -> dict:
    """向量 + BM25 的 RRF 融合召回。

    ``vector_hits`` / ``lexical_hits`` 允许调用方注入已有召回结果（离线测试或
    已算过向量的链路），避免重复 embed。返回 dict 而不是裸 list，附带每一路的
    排名，便于验收「精确术语查询的混合命中率不低于纯向量」。

    默认 ``alpha=0.5``（等权 RRF）是刻意的：加权项为 ``alpha/(K+vr)`` 与
    ``(1-alpha)/(K+lr)``，当 alpha=0.6、K=60 时，向量路第 10 名得分
    ``0.6/70≈0.0086`` 仍高于词法路第 1 名 ``0.4/61≈0.0066``，纯词法结果会被
    **整条挤出 top-k**（实测精确术语查询的混合召回反而等于纯向量）。
    等权下两路第 1 名同分、第 10 名同分，席位自然交替；alpha 仍可显式表达
    对某一路的偏好，但默认不再系统性压制词法召回。
    """
    k = max(0, min(int(k), 200))
    if not query.strip() or k == 0:
        return {"items": [], "method": "hybrid_rrf", "alpha": alpha, "vector": 0, "lexical": 0}
    cand = max(k * 5, 20)
    if vector_hits is None:
        vector_hits = _vector_hits(query, cand, topic, vec)
    if lexical_hits is None:
        lexical_hits = bm25_search(query, k=cand, topic=topic)
    vector_hits = list(vector_hits or [])
    lexical_hits = list(lexical_hits or [])
    alpha = min(1.0, max(0.0, float(alpha)))
    fused: dict[str, dict] = {}
    for rank, h in enumerate(vector_hits, 1):
        pid = str(h.get("id") or "")
        if not pid:
            continue
        e = fused.setdefault(pid, {"item": dict(h), "vr": None, "lr": None})
        e["vr"] = rank
        e.setdefault("item", {}).update({kk: vv for kk, vv in h.items() if kk != "score"})
        e["item"]["vec_score"] = round(float(h.get("score") or 0.0), 4)
    for rank, h in enumerate(lexical_hits, 1):
        pid = str(h.get("id") or "")
        if not pid:
            continue
        e = fused.setdefault(pid, {"item": dict(h), "vr": None, "lr": None})
        e["lr"] = rank
        for kk, vv in h.items():
            if kk not in ("score", "bm25"):
                e["item"].setdefault(kk, vv)
        e["item"]["bm25_score"] = round(float(h.get("bm25") or 0.0), 10)
    out = []
    for pid, e in fused.items():
        vr, lr = e["vr"], e["lr"]
        raw = (alpha / (RRF_K + vr) if vr else 0.0) + \
              ((1.0 - alpha) / (RRF_K + lr) if lr else 0.0)
        item = e["item"]
        item["rrf_score"] = round(raw, 8)
        item["match"] = ("both" if vr and lr else "vector" if vr else "lexical")
        item["vector_rank"] = vr
        item["lexical_rank"] = lr
        out.append(item)
    ceiling = max((x["rrf_score"] for x in out), default=0.0) or 1.0
    for x in out:
        x["score"] = round(x["rrf_score"] / ceiling, 4)
    out.sort(key=lambda x: (-x["score"], x["vector_rank"] or 9999, x["lexical_rank"] or 9999))
    out = out[:k]
    return {
        "items": out, "method": "hybrid_rrf", "rrf_k": RRF_K, "alpha": alpha,
        "candidate_k": cand, "vector": len(vector_hits), "lexical": len(lexical_hits),
        "both": sum(1 for x in out if x["match"] == "both"),
        "fts_available": available(),
    }
