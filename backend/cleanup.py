"""Reversible exact or preview-confirmed semantic cleanup with full-library coverage."""
from __future__ import annotations
from collections import defaultdict
import json
import hashlib

import numpy as np
import math
import re
import sqlite3
import threading
import time
import unicodedata
import uuid

from . import memory, tasks


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").strip()
    # Case, numbers, mathematical symbols and code indentation can change a fact.
    if "```" in text or re.search(r"(?m)^ {2,}\S", text):
        return text
    return " ".join(text.split())


def state() -> dict:
    r = memory.conn().execute("SELECT id,removed,created_at FROM cleanup_runs WHERE restored_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    return {"available": bool(r), **(dict(r) if r else {})}


def _invalidate() -> None:
    memory._invalidate()
    memory._bump_search_cache()
    memory._bump_version()


def _public(row):
    return {k: v for k, v in row.items() if k != "embedding"}


_STALE_MSG = "知识库内容或阈值已变化，请重新预览后确认清理"

#: 预览算出的计划按 (阈值, 主题, token) 暂存，确认清理时直接复用，免去第二次 O(n²)。
#: token 是全部依赖输入（行内容 + 向量 + 阈值 + 主题 + 别名表）的指纹，写库前在
#: 锁内重算指纹复验，所以复用的计划一定与「此刻的库」逐字对应；进程内只留最近 2 份。
_plan_cache_lock = threading.Lock()
_plan_cache: dict[tuple, tuple[int, int, dict]] = {}
_PLAN_CACHE_MAX = 2


def _stale() -> dict:
    return {"ok": False, "stale": True, "msg": _STALE_MSG}


def _check_threshold(threshold) -> float:
    threshold = float(threshold)
    if not math.isfinite(threshold) or not .5 <= threshold <= 1.:
        raise ValueError("相似度阈值必须介于50%和100%之间")
    return threshold


def _knowledge_query(topic):
    return ("SELECT * FROM knowledge" + (" WHERE topic=?" if topic else "") + " ORDER BY id",
            (topic,) if topic else ())


def _aliases(c) -> list:
    """别名表快照：它决定跨主题分组（B2），必须进指纹，否则改别名后预览与执行会对不上。"""
    try:
        return [[r[0], r[1]] for r in c.execute("SELECT alias,canonical FROM topic_aliases ORDER BY alias")]
    except sqlite3.Error:
        return []


def _token_digest(threshold, topic, rows, aliases) -> str:
    """预览 token。无别名时与旧版逐字节一致；有别名时追加别名快照。

    rows 可以是生成器：锁内复验流式读取，不把整库向量物化进内存。
    aliases 可以是无参函数：复验时在行读完后再取，与行处于同一写事务快照。
    """
    digest = hashlib.sha256(json.dumps([threshold, topic], ensure_ascii=False).encode())
    for row in rows:
        digest.update(json.dumps(_public(row), sort_keys=True, ensure_ascii=False).encode())
        digest.update(row["embedding"] or b'')
    if callable(aliases):
        aliases = aliases()
    if aliases:
        digest.update(b"\x00topic_aliases\x00" + json.dumps(aliases, ensure_ascii=False).encode())
    return digest.hexdigest()


def _token_now(c, threshold, topic) -> str:
    """在调用方的写事务里重算 token：O(n) 读 + 哈希，不做任何相似度计算。"""
    sql, params = _knowledge_query(topic)
    rows = (dict(r) for r in c.execute(sql, params))
    return _token_digest(threshold, topic, rows, lambda: _aliases(c))


def _plan(threshold=.9, topic=None):
    from .knowledge_dedup import preference
    threshold = _check_threshold(threshold)
    c = memory.conn()
    # 别名先于行读取：两次读取之间若有人改库，锁内复验时指纹必然对不上 → stale。
    aliases = _aliases(c)
    sql, params = _knowledge_query(topic)
    rows = [dict(r) for r in c.execute(sql, params)]
    token = _token_digest(threshold, topic, rows, aliases)
    # B2：显式登记过别名的主题按规范学科主题分组，让跨书近重复能落在
    # 同一组里；没登记的主题仍是原字符串，既有去重语义不变。
    # 每个不同主题只查一次别名表（原来每行一条 SELECT，1.7 万行就是 1.7 万次）。
    group_keys: dict = {}

    def group_of(raw_topic):
        if raw_topic not in group_keys:
            try:
                from . import topics
                group_keys[raw_topic] = topics.group_key(raw_topic)
            except Exception:  # noqa: BLE001  词表不可用时退回原分组
                group_keys[raw_topic] = raw_topic
        return group_keys[raw_topic]

    # Similar wording in distinct topics can have different meanings.
    groups = defaultdict(list)
    invalid = 0
    for row in rows:
        blob = row["embedding"]
        if not blob or len(blob) % 4:
            invalid += 1
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(vec).all() or not math.isfinite(norm) or norm == 0:
            invalid += 1
            continue
        groups[(group_of(row['topic']), len(vec))].append((row, vec / norm))
    items, drops = [], {}
    for records in groups.values():
        records.sort(key=lambda pair: preference(pair[0]))
        matrix = np.stack([v for _, v in records])
        retained = []
        # Greedy representatives: a deleted item can never delete another item.
        # Each block computes against all candidates with bounded working memory.
        for start in range(0, len(records), 128):
            scores = matrix[start:start + 128] @ matrix.T
            for offset, (row, _) in enumerate(records[start:start + 128]):
                hits = [j for j in retained if scores[offset, j] >= threshold - 1e-7]
                if not hits:
                    retained.append(start + offset)
                    continue
                j = max(hits, key=lambda j: scores[offset, j])
                kept = records[j][0]
                drops[row["id"]] = kept["id"]
                items.append({"keep": _public(kept), "drop": _public(row),
                              "sim": round(min(1., float(scores[offset, j])), 6),
                              "reason": "补充信息更完整" if
                              (len(_normal(kept["detail"])), len(_normal(kept["claim"]))) >
                              (len(_normal(row["detail"])), len(_normal(row["claim"]))) else "内容完整度相同，优先保留更早创建项"})
    return {"ok": True, "threshold": threshold, "topic": topic, "token": token,
            "scanned": len(rows), "invalid": invalid, "removed": len(drops),
            "remaining": len(rows) - len(drops), "groups": len(set(drops.values())), "items": items}, drops


def _remember(plan: dict, drops: dict) -> None:
    key = (plan["threshold"], plan["topic"], plan["token"])
    with _plan_cache_lock:
        _plan_cache.pop(key, None)
        _plan_cache[key] = (plan["scanned"], plan["groups"], dict(drops))
        while len(_plan_cache) > _PLAN_CACHE_MAX:
            _plan_cache.pop(next(iter(_plan_cache)))


def _recall(threshold, topic, token):
    with _plan_cache_lock:
        hit = _plan_cache.get((threshold, topic, token))
        return None if hit is None else (hit[0], hit[1], dict(hit[2]))


def _forget_plans() -> None:
    with _plan_cache_lock:
        _plan_cache.clear()


def preview(threshold=.9, topic=None):
    # _plan 是数十秒级 CPU/内存作业，纯只读却持有全局写锁会让所有 API
    # 阻塞、前端 6 秒轮询全部超时。预览不写库，无需这把锁。
    plan, drops = _plan(threshold, topic)
    _remember(plan, drops)
    return plan


# ---------------------------------------------------------------------------
# 精确清理 / 等价清理：计算本身可在锁内廉价重做，所以锁外先算、锁内复验，
# 复验失败就按最新快照在写事务里重算（等价于旧实现「锁内算」）。
# ---------------------------------------------------------------------------
def _exact_rows(c, topic=None):
    # 旧语义：精确清理恒扫全库，忽略 topic。
    return [dict(r) for r in c.execute(
        "SELECT id,topic,claim,detail,confidence,source_url,created_at,length(embedding) embedding_bytes FROM knowledge")]


def _exact_drops(rows):
    groups = defaultdict(list)
    for row in rows:
        key = (row["topic"], _normal(row["claim"]), _normal(row["detail"]), row["embedding_bytes"])
        groups[key].append(row)
    drops, count = {}, 0
    for members in groups.values():
        if len(members) < 2:
            continue
        # Preserve the established exact-cleanup preference for old clients.
        members = sorted(members, key=lambda p: (-(p["confidence"] or 0.), p["created_at"] or 0., p["id"]))
        drops.update({p["id"]: members[0]["id"] for p in members[1:]})
        count += 1
    return drops, count


def _equivalent_rows(c, topic=None):
    return [dict(r) for r in c.execute('SELECT * FROM knowledge' + (' WHERE topic=?' if topic else ''),
                                       (topic,) if topic else ())]


def _equivalent_drops(rows):
    from .knowledge_dedup import equivalent, normal, preference
    groups = defaultdict(list)
    for row in rows:
        groups[normal(row['claim'])].append(row)
    drops = {}
    for candidates in groups.values():
        retained = []
        for row in sorted(candidates, key=preference):
            keep = next((p for p in retained if equivalent(p, row)), None)
            if keep is None:
                retained.append(row)
            else:
                drops[row['id']] = keep['id']
    return drops, len(set(drops.values()))


def _rows_digest(rows) -> str:
    """快照水位：覆盖计算所依赖的全部列（含向量字节），任何直写 SQL 的改动都会改变它。

    不用 memory.version()：governance / topics / review_pipeline / library 等模块
    直接写 knowledge 而不 bump 版本号，版本号挡不住它们。
    """
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(_public(row), sort_keys=True, ensure_ascii=False).encode())
        digest.update(row.get("embedding") or b"")
        digest.update(b"\x1e")
    return digest.hexdigest()


def _begin_immediate(c) -> None:
    """复验与写入放进同一个写事务：其间任何连接（含不拿 memory._lock 的直写）都无法插队。"""
    if c.in_transaction:
        c.commit()  # 与旧实现 `with c:` 的提交语义一致
    c.execute("BEGIN IMMEDIATE")


def _archive(c, drops: dict) -> str:
    run_id = uuid.uuid4().hex[:16]
    c.execute("INSERT INTO cleanup_runs VALUES (?,?,?,NULL)", (run_id, time.time(), len(drops)))
    ids = list(drops)
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = c.execute(f"SELECT * FROM knowledge WHERE id IN ({placeholders})", batch).fetchall()
        for record in rows:
            point = dict(record)
            embedding = point.pop("embedding", None)
            c.execute("INSERT INTO cleanup_archive VALUES (?,?,?,?,?)", (
                run_id, point["id"], drops[point["id"]], json.dumps(point, ensure_ascii=False), embedding,
            ))
        c.execute(f"DELETE FROM knowledge WHERE id IN ({placeholders})", batch)
    return run_id


def _noop(scanned: int) -> dict:
    return {"ok": True, "removed": 0, "groups": 0, "scanned": scanned, "remaining": scanned, "undo": state()}


def _apply(revalidate) -> dict:
    """持锁只做「复验 → 归档 → 删除」。

    revalidate(c) 在写事务里执行：返回 (scanned, groups, drops) 继续写库；返回 None 表示
    快照已过期（预览路径据此回 stale）。
    """
    with memory._lock:
        c = memory.conn()
        _begin_immediate(c)
        try:
            planned = revalidate(c)
            if planned is None:
                c.rollback()
                return _stale()
            scanned, count, drops = planned
            if not drops:
                c.rollback()
                return _noop(scanned)
            run_id = _archive(c, drops)
            c.commit()
        except BaseException:
            c.rollback()
            raise
        _invalidate()
    _forget_plans()
    memory.log_event("ok", f"重复知识清理：归档 {len(drops)} 条，保留 {scanned - len(drops)} 条；可撤销", {"run_id": run_id})
    return {"ok": True, "removed": len(drops), "groups": count, "scanned": scanned, "remaining": scanned - len(drops), "undo": state()}


def _clean_previewed(threshold: float, token, topic) -> dict:
    if not token:
        return _stale()
    planned = _recall(threshold, topic, token)
    if planned is None:
        # 缓存未命中（重启 / 换了阈值 / 预览来自别处）：O(n²) 在锁外算，此间其他写入照常。
        plan, drops = _plan(threshold, topic)
        if token != plan["token"]:
            return _stale()
        planned = (plan["scanned"], plan["groups"], drops)

    def revalidate(c):
        return planned if _token_now(c, threshold, topic) == token else None

    return _apply(revalidate)


def _clean_recomputable(fetch, compute, topic) -> dict:
    rows = fetch(memory.conn(), topic)
    watermark = _rows_digest(rows)
    drops, count = compute(rows)
    scanned = len(rows)
    if not drops:
        return _noop(scanned)

    def revalidate(c):
        fresh = fetch(c, topic)
        if _rows_digest(fresh) == watermark:
            return scanned, count, drops
        # 锁外计算期间库变了：在写事务里按最新快照重算，结果与旧版「锁内算」一致。
        fresh_drops, fresh_count = compute(fresh)
        return len(fresh), fresh_count, fresh_drops

    return _apply(revalidate)


@tasks.exclusive("重复知识清理")
def clean(threshold=None, token=None, topic=None, equivalent_only=False):
    """重计算全部移到 memory._lock 之外，锁内只做 O(n) 水位复验 + 写库。

    旧实现整段持全局写锁跑 O(n²) 相似度矩阵，期间所有写 API 阻塞、前端轮询超时。
    """
    if threshold is not None:
        return _clean_previewed(_check_threshold(threshold), token, topic)
    if equivalent_only:
        return _clean_recomputable(_equivalent_rows, _equivalent_drops, topic)
    return _clean_recomputable(_exact_rows, _exact_drops, None)


@tasks.exclusive("恢复重复知识")
def undo() -> dict:
    with memory._lock:
        c = memory.conn()
        last = state()
        if not last["available"]:
            return {"ok": False, "msg": "没有可撤销的清理记录"}
        columns = {r[1] for r in c.execute("PRAGMA table_info(knowledge)")}
        restored = 0
        with c:
            for row in c.execute("SELECT * FROM cleanup_archive WHERE run_id=?", (last["id"],)).fetchall():
                point = json.loads(row["row_json"])
                point["embedding"] = row["embedding"]
                names = [k for k in point if k in columns]
                cursor = c.execute(f"INSERT OR IGNORE INTO knowledge ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", [point[k] for k in names])
                restored += cursor.rowcount
            c.execute("UPDATE cleanup_runs SET restored_at=? WHERE id=?", (time.time(), last["id"]))
        _invalidate()
    memory.log_event("ok", f"已撤销重复知识清理，恢复 {restored} 条")
    return {"ok": True, "restored": restored, "undo": state()}


def archived_point(pid: str) -> dict | None:
    row = memory.conn().execute(
        "SELECT a.row_json,a.kept_id FROM cleanup_archive a JOIN cleanup_runs r ON r.id=a.run_id "
        "WHERE a.point_id=? AND r.restored_at IS NULL ORDER BY r.created_at DESC LIMIT 1", (pid,)
    ).fetchone()
    if not row:
        return None
    point = json.loads(row["row_json"])
    try:
        point["tags"] = json.loads(point.get("tags") or "[]")
    except (ValueError, TypeError):
        point["tags"] = []
    canonical = row["kept_id"]
    seen = {pid}
    while canonical not in seen:
        seen.add(canonical)
        next_row = memory.conn().execute(
            "SELECT a.kept_id FROM cleanup_archive a JOIN cleanup_runs r ON r.id=a.run_id "
            "WHERE a.point_id=? AND r.restored_at IS NULL ORDER BY r.created_at DESC LIMIT 1", (canonical,)
        ).fetchone()
        if not next_row:
            break
        canonical = next_row[0]
    return point | {"archived": True, "canonical_id": canonical}


def sources(pid: str) -> list[dict]:
    rows = memory.conn().execute(
        "SELECT a.point_id,a.row_json FROM cleanup_archive a JOIN cleanup_runs r ON r.id=a.run_id "
        "WHERE a.kept_id=? AND r.restored_at IS NULL", (pid,)
    )
    result = []
    for row in rows:
        point = json.loads(row["row_json"])
        result.append({k: point.get(k, "") for k in ("id", "source_url", "source_title", "book_id")})
    return result
