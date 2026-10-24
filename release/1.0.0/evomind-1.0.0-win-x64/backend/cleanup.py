"""Reversible exact or preview-confirmed semantic cleanup with full-library coverage."""
from __future__ import annotations
from collections import defaultdict
import json
import hashlib

import numpy as np
import math
import re
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


def _plan(threshold=.9, topic=None):
    from .knowledge_dedup import preference
    threshold = float(threshold)
    if not math.isfinite(threshold) or not .5 <= threshold <= 1.:
        raise ValueError("相似度阈值必须介于50%和100%之间")
    c = memory.conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM knowledge" + (" WHERE topic=?" if topic else "") +
                                      " ORDER BY id", (topic,) if topic else ())]
    fingerprint = hashlib.sha256(json.dumps([threshold, topic], ensure_ascii=False).encode())
    for row in rows:
        fingerprint.update(json.dumps(_public(row), sort_keys=True, ensure_ascii=False).encode())
        fingerprint.update(row["embedding"] or b'')
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
        groups[(row['topic'], len(vec))].append((row, vec / norm))
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
    return {"ok": True, "threshold": threshold, "topic": topic, "token": fingerprint.hexdigest(),
            "scanned": len(rows), "invalid": invalid, "removed": len(drops),
            "remaining": len(rows) - len(drops), "groups": len(set(drops.values())), "items": items}, drops


def preview(threshold=.9, topic=None):
    with memory._lock:
        return _plan(threshold, topic)[0]


@tasks.exclusive("重复知识清理")
def clean(threshold=None, token=None, topic=None, equivalent_only=False):
    with memory._lock:
        c = memory.conn()
        if threshold is not None:
            plan, drops = _plan(threshold, topic)
            if not token or token != plan["token"]:
                return {"ok": False, "stale": True, "msg": "知识库内容或阈值已变化，请重新预览后确认清理"}
            scanned, count = plan["scanned"], plan["groups"]
        elif equivalent_only:
            from .knowledge_dedup import equivalent, normal, preference
            rows = [dict(r) for r in c.execute('SELECT * FROM knowledge' + (' WHERE topic=?' if topic else ''),
                                               (topic,) if topic else ())]
            scanned = len(rows)
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
            count = len(set(drops.values()))
        else:
            groups = defaultdict(list)
            scanned = 0
            for record in c.execute("SELECT id,topic,claim,detail,confidence,source_url,created_at,length(embedding) embedding_bytes FROM knowledge"):
                row = dict(record)
                scanned += 1
                key = (row["topic"], _normal(row["claim"]), _normal(row["detail"]), row["embedding_bytes"])
                groups[key].append(row)
            drops, count = {}, 0
            for rows in groups.values():
                if len(rows) < 2:
                    continue
                # Preserve the established exact-cleanup preference for old clients.
                rows.sort(key=lambda p: (-(p["confidence"] or 0.), p["created_at"] or 0., p["id"]))
                drops.update({p["id"]: rows[0]["id"] for p in rows[1:]})
                count += 1
        if not drops:
            return {"ok": True, "removed": 0, "groups": 0, "scanned": scanned, "remaining": scanned, "undo": state()}
        run_id = uuid.uuid4().hex[:16]
        with c:
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
        _invalidate()
    memory.log_event("ok", f"重复知识清理：归档 {len(drops)} 条，保留 {scanned - len(drops)} 条；可撤销", {"run_id": run_id})
    return {"ok": True, "removed": len(drops), "groups": count, "scanned": scanned, "remaining": scanned - len(drops), "undo": state()}


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
