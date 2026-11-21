"""A3 · 向量近似索引层（FAISS HNSW，可选、默认关闭）。

设计要点（与 search_index.py 同一套「纯派生数据」哲学）
----------------------------------------------------
* 索引是 **纯派生数据**：放在 ``data/knowledge/vector_ann.index``（FAISS 索引）
  + ``vector_ann.ids.json``（内部行号 → knowledge.rowid / id 的映射）。删掉随时
  可重建，主库 schema 一行都不用改，也不给老库迁移增加启动风险。
* 索引只在 ``config.vector_ann_enabled == True`` 时参与检索；关闭时本模块全部
  函数退化为「不可用」，``hybrid_search`` 自动回退到 ``memory.search`` 的暴力
  余弦，与改造前行为完全一致（零行为改变）。
* 读取主库 embedding 一律走 ``sqlite3.dbapi2.connect(mode=ro)``，与主库连接
  生命周期隔离；写入只落在派生索引文件，真实库零写入。
* 近似代价：评测（bench_ann.py）中 HNSW 的 recall@10 与暴力余弦一致（近重复
  查询口径），但通用语义召回质量仍取决于人工真值集（当前缺失）。默认关闭的
  用意正是「不替用户做主」：21k 规模暴力已 ~1ms，没必要冒近似召回风险；仅当
  语料达 100k 级且你确认要换更快的近似路径时再开启。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config, evidence

try:
    import numpy as np
    import faiss

    _HAVE_FAISS = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    faiss = None  # type: ignore
    _HAVE_FAISS = False

_lock = threading.Lock()
_available: bool | None = None

# 内存中的索引缓存（按文件 mtime 失效），避免每次查询都重读磁盘。
_cache_idx = None
_cache_ids: list[str] = []
_cache_mtime: int | None = None

#: HNSW 每个节点的邻居数 M。32 是 bge-m3(1024 维) 下的稳妥取值：召回接近精确，
#: 内存占用约 2·M·4·N 字节（21k 约 21MB，100k 约 102MB）。
_HNSW_M = 32


def _index_path() -> Path:
    return Path(config.KNOWLEDGE) / "vector_ann.index"


def _ids_path() -> Path:
    return Path(config.KNOWLEDGE) / "vector_ann.ids.json"


def reset_cache() -> None:
    """清空可用性与索引缓存。测试切换 config 开关或重建索引后调用。"""
    global _available, _cache_idx, _cache_ids, _cache_mtime
    _available = None
    _cache_idx = None
    _cache_ids = []
    _cache_mtime = None


def enabled() -> bool:
    try:
        return bool(config.get("vector_ann_enabled"))
    except Exception:  # noqa: BLE001
        return False


def available() -> bool:
    """faiss 可用且已开启才算可用；任一不满足则上层回退暴力余弦。"""
    global _available
    if _available is None:
        _available = _HAVE_FAISS and enabled()
    return bool(_available)


def _main_conn() -> sqlite3.Connection | None:
    try:
        p = Path(config.DB_FILE).resolve()
        if not p.exists():
            return None
        c = sqlite3.dbapi2.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30.0)
        c.row_factory = sqlite3.Row
        return c
    except (sqlite3.Error, OSError):
        return None


def _load_id_map() -> list[str]:
    p = _ids_path()
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("ids", [])
    except Exception:  # noqa: BLE001
        return []


def _source_signature(c: "sqlite3.Connection | None" = None) -> dict:
    """主库「候选向量行」的廉价签名：行数 + 字节总量 + 最大 rowid。

    旧实现拿索引条数与全表 COUNT(*) 对比，而索引只收录有效向量行；库里
    只要存在一条无 embedding / 非法向量的行，stale 就恒为 True，每次查询
    都全量重建。签名在建索引时写进 id map，status() 一次聚合查询即可复验。
    """
    own = c is None
    if own:
        c = _main_conn()
    if c is None:
        return {"count": 0, "bytes": 0, "max_rid": 0}
    try:
        r = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(embedding)),0), "
            "COALESCE(MAX(rowid),0) FROM knowledge "
            "WHERE embedding IS NOT NULL AND length(embedding)>0"
        ).fetchone()
        return {"count": int(r[0]), "bytes": int(r[1]), "max_rid": int(r[2])}
    finally:
        if own:
            c.close()


def _load_source_signature() -> dict | None:
    p = _ids_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        sig = d.get("source")
        return sig if isinstance(sig, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _save_id_map(ids: list[str], source: dict | None = None) -> None:
    payload: dict[str, Any] = {"ids": ids}
    if source is not None:
        payload["source"] = source
    _ids_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_embeddings() -> tuple[list[int], list[str], "np.ndarray"]:
    """从主库读出已归一化的 embedding，按 faiss 内部顺序对齐 rowid / id。"""
    c = _main_conn()
    if c is None or np is None:
        return [], [], np.empty((0, 0), dtype=np.float32)
    try:
        rows = c.execute(
            "SELECT rowid, id, embedding FROM knowledge WHERE embedding IS NOT NULL"
        ).fetchall()
        rowids, ids, vecs = [], [], []
        for r in rows:
            blob = r["embedding"]
            if not blob:
                continue
            v = np.frombuffer(blob, dtype=np.float32)
            if not v.size or not np.isfinite(v).all():
                continue
            n = float(np.linalg.norm(v))
            if not np.isfinite(n) or n == 0:
                continue
            rowids.append(int(r["rowid"]))
            ids.append(str(r["id"]))
            vecs.append(v / n)
        if not vecs:
            return [], [], np.empty((0, 0), dtype=np.float32)
        return rowids, ids, np.stack(vecs).astype(np.float32)
    finally:
        c.close()


def rebuild(*, ef_construction: int = 200, only_if_stale: bool = False) -> dict:
    """从主库全量重建 HNSW 向量索引。幂等；返回可核对计数。"""
    t0 = time.perf_counter()
    if not available():
        return {"available": False, "reason": "faiss 不可用或未开启", "indexed": 0}
    with _lock:
        if only_if_stale:
            st = status()
            if not st.get("stale"):
                return {"available": True, "indexed": st.get("indexed", 0),
                        "rebuilt": False, "reason": "并发重建已完成",
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
        # 先取签名再读向量：两者之间若有写入，签名偏旧会让 status 判 stale
        # 并触发一次重建，宁可多重建也不漏召。
        source_sig = _source_signature()
        reset_cache()
        rowids, ids, X = _read_embeddings()
        if X.shape[0] == 0:
            # 主库没有有效向量：清空任何旧索引，避免留下过期数据误导。
            for p in (_index_path(), _ids_path()):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            return {"available": True, "indexed": 0, "rebuilt": True,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
        dim = int(X.shape[1])
        idx = faiss.IndexHNSWFlat(dim, _HNSW_M)
        idx.hnsw.efConstruction = ef_construction
        idx.add(X)
        _index_path().parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(idx, str(_index_path()))
        _save_id_map(ids, source_sig)
        # 写盘后让缓存立即失效（mtime 也可能落在同一秒），强制下次查询重载。
        reset_cache()
        return {
            "available": True, "indexed": len(ids), "rebuilt": True,
            "dims": dim, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "path": str(_index_path()),
        }


def status() -> dict:
    if not available() or not _index_path().exists():
        return {"available": False, "indexed": 0, "knowledge_total": 0, "stale": True}
    ids = _load_id_map()
    mc = _main_conn()
    total = 0
    sig = {"count": 0, "bytes": 0, "max_rid": 0}
    if mc is not None:
        try:
            total = int(mc.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0])
            sig = _source_signature(mc)
        finally:
            mc.close()
    stored = _load_source_signature()
    # 老版本 id map 没有 source 签名：判一次过期，重建后自动补齐。
    fresh = stored is not None and stored == sig
    return {
        "available": True, "indexed": len(ids), "knowledge_total": total,
        "vector_rows": sig["count"], "stale": not fresh, "path": str(_index_path()),
    }


def mark_stale() -> None:
    """增量同步后声明向量索引过期，下次查询（ensure_ready）会全量重建一次。

    HNSW 不支持就地删除，宁可显式过期重建，也不让索引悄悄漏召——与
    search_index.mark_stale 同一取舍。
    """
    reset_cache()
    for p in (_index_path(), _ids_path()):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_ready(*, force: bool = False) -> bool:
    if not available():
        return False
    if force:
        rebuild()
        return True
    st = status()
    if st.get("stale"):
        # 与 search_index.ensure_ready 同口径：并发时只重建一次。
        rebuild(only_if_stale=True)
    return True


def _load_index() -> tuple[Any, list[str]]:
    """加载（并按 mtime 缓存）FAISS 索引与 id 映射。"""
    global _cache_idx, _cache_ids, _cache_mtime
    ip = _index_path()
    if not ip.exists():
        reset_cache()
        return None, []
    mtime = ip.stat().st_mtime_ns
    if _cache_idx is not None and _cache_mtime == mtime and _cache_ids:
        return _cache_idx, _cache_ids
    idx = faiss.read_index(str(ip))
    ids = _load_id_map()
    _cache_idx, _cache_ids, _cache_mtime = idx, ids, mtime
    return idx, ids


def search(vec: "list[float] | np.ndarray", k: int = 10,
           *, ef_search: int | None = None) -> list[str] | None:
    """返回 top-k 的 knowledge.id 列表；不可用或 k<=0 时返回 None（上层回退）。"""
    k = max(0, min(int(k), 1000))
    if not available() or k == 0 or np is None:
        return None
    try:
        ip = _index_path()
        if not ip.exists():
            ensure_ready()
            if not ip.exists():
                return None
        with _lock:
            idx, ids = _load_index()
            if idx is None or not ids:
                return None
            if ef_search is not None:
                idx.hnsw.efSearch = int(ef_search)
            q = np.asarray(vec, dtype=np.float32)
            if q.ndim != 1 or not q.size:
                return None
            n = float(np.linalg.norm(q))
            if not np.isfinite(n) or n == 0:
                return None
            q = (q / n).reshape(1, -1).astype(np.float32)
            if q.shape[1] != idx.d:
                return None
            _, I = idx.search(q, k)
        out = []
        for x in I[0]:
            if 0 <= int(x) < len(ids):
                out.append(ids[int(x)])
        return out
    except Exception:  # noqa: BLE001
        return None


def _cosine(vec: "list[float] | np.ndarray", blob: bytes | None) -> float:
    if not blob or np is None:
        return 0.0
    v = np.frombuffer(blob, dtype=np.float32)
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n == 0:
        return 0.0
    q = np.asarray(vec, dtype=np.float32)
    m = float(np.linalg.norm(q))
    if not np.isfinite(m) or m == 0:
        return 0.0
    return float(np.dot(q / m, v / n))


def vector_hits(query: str, k: int, *, topic: str | None = None,
                vec: "list[float] | None" = None) -> "list[dict] | None":
    """与 ``memory.search`` 等价的返回形状，供 ``hybrid_search`` 直接消费。

    返回 ``None`` 表示「本层不可用，请回退 memory.search」。成功则返回
    ``[{id, score, topic, claim, detail, ...}]``，``score`` 为余弦相似度。
    """
    k = max(0, min(int(k), 1000))
    if not available() or k == 0 or np is None:
        return None
    if vec is None:
        try:
            from . import llm

            ev = llm.embed([query])
            vec = ev[0] if ev else None
        except Exception:  # noqa: BLE001
            return None
    if vec is None:
        return None
    pids = search(vec, k, ef_search=int(config.get("vector_ann_ef_search") or 64))
    if pids is None or not pids:
        return None
    mc = _main_conn()
    if mc is None:
        return None
    try:
        ordered = list(dict.fromkeys(str(p) for p in pids))
        found: dict[str, sqlite3.Row] = {}
        # 与 bm25_search 同一口径：400 个一批用 IN 取回，消除 top-k 逐行
        # 单行 SELECT 的 N+1（k=1000 时原本是 1000 次 SQLite 往返）。
        for start in range(0, len(ordered), 400):
            part = ordered[start:start + 400]
            marks = ",".join("?" * len(part))
            sql = (f"SELECT * FROM knowledge WHERE id IN ({marks}) "
                   f"AND {evidence.ACTIVE_FILTER}")
            args: list[Any] = list(part)
            if topic:
                sql += " AND topic=?"
                args.append(topic)
            for r in mc.execute(sql, args):
                found[str(r["id"])] = r
        out: list[dict] = []
        for pid in ordered:
            row = found.get(pid)
            if row is None:
                continue
            blob = row["embedding"]
            d = dict(row)
            d.pop("embedding", None)
            d["score"] = round(float(_cosine(vec, blob)), 4)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except Exception:  # noqa: BLE001
                d["tags"] = []
            out.append(d)
        out.sort(key=lambda x: -x["score"])
        return out[:k]
    finally:
        mc.close()
