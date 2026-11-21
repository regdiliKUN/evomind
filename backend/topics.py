"""B2 · 受控主题词表与别名归一。

原状：真实库 239 个 topic，Top 主题全是「书库 · <书名> (<作者>) (Z-Library)」。
topic 同时承担「学习任务名 / 书籍标识 / 检索过滤 / 去重分组键」四种职责，
于是同一学科的三本书被拆成三个互不相通的分组，跨书近重复永远清不掉。
``evolve._query_subject()`` 里那段专门剥「书库 · 」前缀的正则，本身就是
分类设计失当的自证。

这里不改写任何历史行的 topic（那会让既有检索过滤和向量缓存全部失效），而是：

* ``topic_terms`` 存受控学科主题（可带 parent 做层级）；
* ``topic_aliases`` 存「原始 topic 字符串 → 规范主题」映射；
* ``group_key()`` 让去重分组按规范主题聚合——**仅在显式登记过别名时**生效，
  没登记的主题行为完全不变，因此不会悄悄改变既有语义；
* ``reclassify()`` 支持显式重分类，只 UPDATE topic，不碰 claim/detail/embedding，
  并写 immutable revision 快照。
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Iterable

from . import evidence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_terms (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, parent TEXT DEFAULT '',
    note TEXT DEFAULT '', created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS topic_aliases (
    alias TEXT PRIMARY KEY, canonical TEXT NOT NULL, created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_alias_canonical ON topic_aliases(canonical);
"""

#: 「书库 · XXX (作者) (Z-Library)」这类机器拼出来的主题前后缀。
_BOOK_PREFIX = re.compile(r"^\s*(?:书库|书架|文库)\s*[·\-—:：]\s*")
_TRAIL_PAREN = re.compile(r"\s*[（(][^（）()]{0,80}[）)]\s*$")


def normalize(raw: str) -> str:
    """去掉书名式装饰、压平空白；不做语义猜测，保证确定性。"""
    s = " ".join(str(raw or "").split())
    s = _BOOK_PREFIX.sub("", s)
    # 尾部的 (作者) / (Z-Library) / (出版社) 可能连续出现多个
    for _ in range(4):
        new = _TRAIL_PAREN.sub("", s)
        if new == s:
            break
        s = new
    return s.strip()


def _conn() -> sqlite3.Connection:
    from . import memory
    return memory.conn()


def canonical(topic: str) -> str:
    """显式别名优先；没登记时返回确定性归一化结果（不等于自动改分类）。"""
    raw = " ".join(str(topic or "").split())
    try:
        row = _conn().execute("SELECT canonical FROM topic_aliases WHERE alias=?",
                              (raw,)).fetchone()
    except Exception:  # noqa: BLE001  词表是增强能力，缺表时退回纯规则
        return normalize(raw)
    if row:
        try:
            return str(row["canonical"])
        except (KeyError, TypeError):
            return str(row[0])
    return normalize(raw)


def group_key(topic: str) -> str:
    """去重分组键：只有显式登记过别名的主题才聚到规范主题。

    这样既满足「把 3 本 ZBrush 书归到同一学科主题后跨书近重复可检出」，
    又不会在用户没做任何配置时悄悄改变 cleanup / duplicates 的既有分组。
    """
    raw = " ".join(str(topic or "").split())
    try:
        row = _conn().execute("SELECT canonical FROM topic_aliases WHERE alias=?",
                              (raw,)).fetchone()
    except Exception:  # noqa: BLE001
        return raw
    if row:
        try:
            return str(row["canonical"])
        except (KeyError, TypeError):
            return str(row[0])
    return raw


def expand(canonical_topic: str) -> list[str]:
    """一个规范主题对应的全部原始 topic（含显式别名与归一化后相同的）。"""
    target = str(canonical_topic or "").strip()
    if not target:
        return []
    out: set[str] = {target}
    c = _conn()
    try:
        for r in c.execute("SELECT alias FROM topic_aliases WHERE canonical=?", (target,)):
            try:
                out.add(str(r["alias"]))
            except (KeyError, TypeError):
                out.add(str(r[0]))
    except Exception:  # noqa: BLE001
        pass
    try:
        for r in c.execute("SELECT DISTINCT topic FROM knowledge"):
            raw = str(r["topic"] if "topic" in r.keys() else r[0])
            if normalize(raw) == target:
                out.add(raw)
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


def vocabulary() -> list[dict]:
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM topic_terms ORDER BY parent,label").fetchall()]


def aliases(limit: int = 500) -> list[dict]:
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM topic_aliases ORDER BY canonical,alias LIMIT ?",
        (max(1, min(int(limit), 5000)),)).fetchall()]


def add_term(label: str, *, parent: str = "", note: str = "") -> dict:
    from . import memory
    label = str(label or "").strip()
    if not label:
        raise ValueError("主题名不能为空")
    now = time.time()
    with memory._lock:
        c = _conn()
        with c:
            c.execute(
                "INSERT INTO topic_terms(id,label,parent,note,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "label=excluded.label,parent=excluded.parent,note=excluded.note,"
                "updated_at=excluded.updated_at",
                (label, label, str(parent or ""), str(note or ""), now, now),
            )
    return {"ok": True, "id": label}


def remove_term(label: str) -> dict:
    from . import memory
    with memory._lock:
        c = _conn()
        with c:
            n = c.execute("DELETE FROM topic_terms WHERE id=?", (label,)).rowcount
    return {"ok": True, "removed": n}


def add_alias(alias: str, canonical_topic: str) -> dict:
    from . import memory
    alias = " ".join(str(alias or "").split())
    target = str(canonical_topic or "").strip()
    if not alias or not target:
        raise ValueError("alias 与 canonical 都不能为空")
    with memory._lock:
        c = _conn()
        with c:
            c.execute("INSERT OR REPLACE INTO topic_aliases(alias,canonical,created_at) "
                      "VALUES(?,?,?)", (alias, target, time.time()))
            c.execute("INSERT OR IGNORE INTO topic_terms(id,label,parent,note,created_at,updated_at) "
                      "VALUES(?,?,'','',?,?)", (target, target, time.time(), time.time()))
        memory._bump_version()
    return {"ok": True, "alias": alias, "canonical": target}


def remove_alias(alias: str) -> dict:
    from . import memory
    with memory._lock:
        c = _conn()
        with c:
            n = c.execute("DELETE FROM topic_aliases WHERE alias=?", (alias,)).rowcount
        memory._bump_version()
    return {"ok": True, "removed": n}


def clusters(limit: int = 40) -> dict:
    """把原始主题按规范主题聚合，暴露「书名当主题」的实际规模。

    口径警示（B2 补充验收 F3）：本视图按 ``canonical()`` 聚合，它包含
    ``normalize()`` 规则；而 ``cleanup._plan()`` 实际去重按 ``group_key()``
    聚合，后者**只在显式登记别名后**才改变分组。若不区分这两个口径，界面上
    显示「已合并成同一主题」的组，实际去重仍被拆成多组，会让用户误以为
    跨书近重复问题已解决。因此这里同时返回两个口径，并显式列出
    ``normalize_only_merges``：视图已合并、但去重仍彼此独立的组。
    """
    c = _conn()
    rows = c.execute(
        f"SELECT topic, COUNT(*) n FROM knowledge WHERE {evidence.ACTIVE_FILTER} "
        "GROUP BY topic ORDER BY n DESC"
    ).fetchall()
    grouped: dict[str, dict] = {}
    dedup: dict[str, int] = {}
    alias_registered = 0
    gk_cache: dict[str, str] = {}

    def gk(topic: str) -> str:
        if topic not in gk_cache:
            gk_cache[topic] = group_key(topic)
        return gk_cache[topic]

    for r in rows:
        raw = str(r["topic"] if "topic" in r.keys() else r[0])
        n = int(r["n"] if "n" in r.keys() else r[1])
        key = canonical(raw)
        g = grouped.setdefault(key, {"canonical": key, "points": 0, "raw_topics": []})
        g["points"] += n
        g["raw_topics"].append({"topic": raw, "count": n})
        dk = gk(raw)
        dedup[dk] = dedup.get(dk, 0) + n
        if dk != " ".join(raw.split()):
            alias_registered += 1

    normalize_only: list[dict] = []
    for key, g in grouped.items():
        members = [x["topic"] for x in g["raw_topics"]]
        if len(members) < 2:
            continue
        distinct_dedup_keys = {gk(m) for m in members}
        if len(distinct_dedup_keys) > 1:
            normalize_only.append({
                "canonical": key, "members": members,
                "dedup_groups": len(distinct_dedup_keys), "points": g["points"],
            })
    normalize_only.sort(key=lambda x: -x["points"])

    ordered = sorted(grouped.values(), key=lambda x: -x["points"])
    warning = ""
    if normalize_only:
        warning = (f"有 {len(normalize_only)} 组在视图里按规则归一显示为同一主题，"
                   "但 cleanup 去重仍按原始 topic 分成多组（未登记别名）。"
                   "请先登记别名或执行重分类，再执行去重，否则跨书近重复仍检不出。")
    return {
        "raw_topics": len(rows), "canonical_topics": len(ordered),
        "dedup_groups": len(dedup),
        "alias_registered_topics": alias_registered,
        "vocabulary_terms": len(vocabulary()), "alias_count": len(aliases(limit=5000)),
        "view_basis": "canonical() = 显式别名 + normalize 规则",
        "dedup_basis": "group_key() = 仅显式别名；未登记时保持原始 topic",
        "grouping_consistent": not normalize_only,
        "normalize_only_merges": normalize_only[:20],
        "warning": warning,
        "items": ordered[:max(1, min(int(limit), 200))],
    }


def reclassify(raw_topics: Iterable[str], canonical_topic: str, *,
               dry_run: bool = True, actor: str = "user", reason: str = "") -> dict:
    """显式重分类：只改 topic，claim/detail/embedding 一字不动。

    每次改写都写 immutable revision 快照（kind='reclassify'），并登记别名，
    这样未来查询这个规范主题时能把旧分组一起纳入。
    """
    from . import governance, memory
    raws = [str(x) for x in dict.fromkeys(raw_topics or []) if str(x)]
    target = str(canonical_topic or "").strip()
    if not raws or not target:
        raise ValueError("raw_topics 与 canonical 都不能为空")
    with memory._lock:
        c = _conn()
        marks = ",".join("?" * len(raws))
        rows = [dict(r) for r in c.execute(
            f"SELECT id,topic,claim,detail,length(embedding) AS eb,revision FROM knowledge "
            f"WHERE topic IN ({marks})", raws)]
        out: dict[str, Any] = {
            "ok": True, "dry_run": bool(dry_run), "canonical": target,
            "matched": len(rows), "raw_topics": raws, "changed": 0,
        }
        if dry_run or not rows:
            return out
        now = time.time()
        with c:
            for raw in raws:
                c.execute("INSERT OR REPLACE INTO topic_aliases(alias,canonical,created_at) "
                          "VALUES(?,?,?)", (raw, target, now))
            c.execute("INSERT OR IGNORE INTO topic_terms(id,label,parent,note,created_at,updated_at) "
                      "VALUES(?,?,'','',?,?)", (target, target, now, now))
            for row in rows:
                governance._snapshot(c, row["id"], row, "reclassify",
                                     actor=actor, reason=reason or f"重分类 → {target}")
                c.execute("UPDATE knowledge SET topic=?,updated_at=? WHERE id=?",
                          (target, now, row["id"]))
            out["changed"] = len(rows)
        governance._finish(memory)
    return out
