"""Knowledge evidence: literal citation checks, locators and model provenance.

This module is deliberately dependency-light (config only). It is the single
place where "what counts as evidence" is defined, so the web path, the book
path and the refinement path cannot silently diverge again.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from . import config

#: Bump when the knowledge schema changes. Written into schema_meta by
#: memory._upgrade(), and into backup manifests by backup.export_backup().
SCHEMA_VERSION = 2

#: Historical points keep the marker embedded in detail; new points also store
#: the same string in a dedicated column. Keep both while old rows exist.
#: 这是**写入**规范（带换行，人和模型都好读），不要拿它做历史识别。
EVIDENCE_MARKER = "\n原文依据："

#: 历史行分隔符不统一：真实库实测 3440 条带「原文依据：」的行里，只有 79 条
#: 前面是换行，另外 3361 条前面是空格。用不含前缀空白的错点做**读取/回填**
#: 识别，才能把 16.3% 的历史引文全部抽成可计算的 evidence，而不是只抽 2.3%。
EVIDENCE_ANCHOR = "原文依据："

#: Additive columns. _upgrade() replays these on old databases; SCHEMA uses the
#: same definitions so a fresh database and a migrated database look identical.
KNOWLEDGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("evidence", "TEXT DEFAULT ''"),
    ("evidence_locator", "TEXT DEFAULT ''"),
    ("evidence_hash", "TEXT DEFAULT ''"),
    ("status", "TEXT DEFAULT 'unverified'"),
    ("revision", "INTEGER DEFAULT 1"),
    ("created_by", "TEXT DEFAULT ''"),
    ("model_version", "TEXT DEFAULT ''"),
    ("embed_model_version", "TEXT DEFAULT ''"),
    ("verification", "TEXT DEFAULT ''"),
    ("supersedes", "TEXT DEFAULT ''"),
    ("updated_at", "REAL"),
    ("reviewed_at", "REAL"),
    ("archived_at", "REAL"),
)

#: Statuses that must never reach retrieval, training export or health scoring.
EXCLUDED_STATUSES = frozenset({"archived", "disputed", "superseded"})
#: SQL fragment. COALESCE keeps rows written before the migration searchable.
ACTIVE_FILTER = "COALESCE(status, 'legacy') NOT IN ('archived','disputed','superseded')"

#: 代码里实际写入 _ingest_path 的四个值。配置注释曾把书库写成单个 ``library``，
#: 但真实值是 ``library:book`` / ``library:url``，精确匹配会让灰度白名单静默失效。
KNOWN_INGEST_PATHS: frozenset[str] = frozenset({
    "evolve:web", "library:book", "library:url", "refinement",
})
#: 进程内观测到的通路名（含测试与未来新增路径），用于诊断「配了但没匹配上」。
_OBSERVED_PATHS: set[str] = set()


def evidence_supported(evidence: str, text: str) -> bool:
    """Return True only when evidence literally occurs in text.

    Three checks, strictest first, all still requiring exact characters (only
    whitespace, width and case are tolerated):

      1. whole citation after whitespace folding
      2. whole citation after removing all whitespace
      3. any continuous 16-character window of the citation

    Rule 3 exists because transcribed media sources and book chunks often get
    re-wrapped; it is NOT a semantic similarity check, so paraphrase,
    translation and model-invented quotes still fail.
    """
    if not isinstance(evidence, str) or len(evidence.strip()) < 12:
        return False
    folded_text = re.sub(r"\s+", " ", text).strip().casefold()
    folded_ev = re.sub(r"\s+", " ", evidence).strip().casefold()
    if folded_ev in folded_text:
        return True
    squeezed_text = re.sub(r"\s+", "", text).casefold()
    squeezed_ev = re.sub(r"\s+", "", evidence).casefold()
    if squeezed_ev in squeezed_text:
        return True
    win = min(16, len(squeezed_ev))
    return any(squeezed_ev[i:i + win] in squeezed_text
               for i in range(0, len(squeezed_ev) - win + 1))


def split_detail(detail: str) -> tuple[str, str]:
    """Split a historical citation suffix into ``(detail, evidence)``.

    Both historical prefixes are accepted: ``\\n原文依据：`` and
    `` 原文依据：``. Using EVIDENCE_MARKER here would silently leave 97.7%
    of the legacy citations embedded in detail and invisible to metrics.
    """
    if not isinstance(detail, str):
        return "", ""
    head, marker, tail = detail.rpartition(EVIDENCE_ANCHOR)
    if marker:
        return head.rstrip(), tail.strip()
    return detail, ""


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()


def locator_for(source_url: str, source_text: str, evidence: str) -> str:
    """Stable, human-checkable pointer into the original material.

    Prefer a real character offset when the source text is available; fall back
    to a content hash so a locator always exists even for legacy rows.
    """
    base = (source_url or "").strip()
    if evidence and source_text:
        squeezed = re.sub(r"\s+", "", source_text)
        squeezed_ev = re.sub(r"\s+", "", evidence)
        idx = squeezed.find(squeezed_ev[:16]) if squeezed_ev else -1
        if idx >= 0:
            return f"{base}#char={idx}"
    if evidence:
        return f"{base}#sha256={sha256(evidence)[:16]}"
    return base


def model_versions() -> dict[str, str]:
    """Which models produced this point (recorded, never inferred later)."""
    try:
        return {
            "model_version": str(config.get("model") or ""),
            "embed_model_version": str(config.get("embed_model") or ""),
        }
    except Exception:  # noqa: BLE001  config may be unavailable in isolation tests
        return {"model_version": "", "embed_model_version": ""}


# 证据策略拒绝统计（进程内）。用途：在把 require_evidence 切到 True 之前，
# 先量出「每天会拒掉多少条、集中在哪条入库通路」，而不是拿真实导入流程做赌注。
_REJECT_STATS: dict[str, Any] = {"fabricated": 0, "no_evidence": 0, "by_path": {}}


def reject_stats() -> dict:
    """只读快照，供 /api/analytics/evidence-policy 观察拒绝率。"""
    return {
        "fabricated": _REJECT_STATS["fabricated"],
        "no_evidence": _REJECT_STATS["no_evidence"],
        "by_path": dict(_REJECT_STATS["by_path"]),
        "total": _REJECT_STATS["fabricated"] + _REJECT_STATS["no_evidence"],
    }


def reset_reject_stats() -> None:
    _REJECT_STATS.update(fabricated=0, no_evidence=0, by_path={})


def reset_observed_paths() -> None:
    """仅供测试隔离：清空运行时观测到的通路名。"""
    _OBSERVED_PATHS.clear()


def _path_matches(configured: str, ingest_path: str) -> bool:
    """精确匹配，或按 ``组名:子通路`` 的组前缀匹配。

    ``library`` 因此能一次覆盖 ``library:book`` 与 ``library:url``；
    ``evolve`` 能覆盖 ``evolve:web``。精确值仍然照常生效。
    """
    if not configured:
        return False
    if configured == ingest_path:
        return True
    return bool(ingest_path) and ingest_path.startswith(configured + ":")


def policy_diagnostics() -> dict:
    """诊断灰度白名单是否真的能匹配到入库通路，避免「配了却静默不生效」。"""
    try:
        raw = config.get("require_evidence_paths", None)
        configured = ([str(x).strip() for x in raw if str(x).strip()]
                      if isinstance(raw, (list, tuple, set)) else [])
    except Exception:  # noqa: BLE001  隔离测试里 config 可能不可用
        configured = []
    known = set(KNOWN_INGEST_PATHS) | set(_OBSERVED_PATHS)

    def _resolves(item: str) -> bool:
        return item in known or any(p.startswith(item + ":") for p in known)

    bad = [p for p in configured if not _resolves(p)]
    return {
        "configured_paths": configured,
        "known_paths": sorted(KNOWN_INGEST_PATHS),
        "observed_paths": sorted(_OBSERVED_PATHS),
        "unmatched_configured": bad,
        "ok": not bad,
        "hint": ("白名单里有无法匹配任何入库通路的名称，灰度不会生效；"
                 "可用组名 library / evolve 或精确值 library:book、library:url、"
                 "evolve:web、refinement。") if bad else "",
    }


def _note_reject(kind: str, path: str) -> None:
    _REJECT_STATS[kind] = int(_REJECT_STATS.get(kind, 0)) + 1
    bucket = _REJECT_STATS["by_path"].setdefault(
        path or "(unknown)", {"fabricated": 0, "no_evidence": 0})
    bucket[kind] = bucket.get(kind, 0) + 1


def evidence_policy(ingest_path: str) -> tuple[bool, bool]:
    """返回 ``(enforce, dry_run)``，支持按入库通路灰度收紧。

    优先级：``require_evidence=True`` 全局强制 > ``require_evidence_paths``
    白名单 > 默认放行。``require_evidence_dry_run=True`` 时候选仍会入库，但
    打上 ``verification='policy_would_reject'``，可以先量出拒绝率再真正切换。
    历史库 82.8% 来自书库且零引文，一刀切会让导入停产，所以默认全放行。

    白名单支持组前缀：``library`` 覆盖 ``library:book`` / ``library:url``，
    ``evolve`` 覆盖 ``evolve:web``。配错的名称不会报错，但会由
    ``policy_diagnostics()`` 报出来，避免静默失效。
    """
    path = str(ingest_path or "")
    if path:
        _OBSERVED_PATHS.add(path)
    try:
        if bool(config.get("require_evidence", False)):
            enforce = True
        else:
            paths = config.get("require_evidence_paths", None)
            wanted = ({str(x).strip() for x in paths if str(x).strip()}
                      if isinstance(paths, (list, tuple, set)) else set())
            enforce = any(_path_matches(p, path) for p in wanted)
        return bool(enforce), bool(config.get("require_evidence_dry_run", False))
    except Exception:  # noqa: BLE001  隔离测试里 config 可能不可用
        return False, False


def prepare_candidate(point: dict, *, enforce: bool | None = None) -> dict | None:
    """Normalise one candidate before it reaches the ingestion gateway.

    Returns an enriched copy, or None when the candidate must be rejected:
      * a citation that is not literally present in the supplied source text
        (fabricated evidence is worse than no evidence at all)
      * no citation at all while the evidence policy enforces it, unless
        ``require_evidence_dry_run`` is on (then it is tagged instead)

    ``detail`` keeps its historical marker so evolve._verify_batch() and older
    tests continue to work unchanged.
    """
    out = dict(point)
    source_text = str(out.pop("_source_text", "") or "")
    ingest_path = str(out.pop("_ingest_path", "") or "")

    detail = str(out.get("detail") or "")
    stored = str(out.get("evidence") or "").strip()
    from_detail = split_detail(detail)[1]
    evidence = stored or from_detail

    if enforce is None:
        enforce, dry_run = evidence_policy(ingest_path)
    else:
        dry_run = False          # 显式传参的场景（含测试）不做观察模式

    if evidence:
        if source_text and not evidence_supported(evidence, source_text):
            _note_reject("fabricated", ingest_path)
            return None
        verification = "literal_pass" if source_text else "unverified"
        status = "verified" if source_text else "unverified"
    else:
        if enforce:
            _note_reject("no_evidence", ingest_path)
            if not dry_run:
                return None
            # 观察模式：照常入库，但如实标记「按新策略本该被拒」。
            verification = "policy_would_reject"
            status = "unverified"
        else:
            verification = "none"
            status = "unverified"

    versions = model_versions()
    out.update(
        evidence=evidence[:2000],
        evidence_locator=str(out.get("evidence_locator") or
                             locator_for(str(out.get("source_url") or ""), source_text, evidence)),
        evidence_hash=sha256(evidence) if evidence else "",
        status=str(out.get("status") or status),
        verification=str(out.get("verification") or verification),
        revision=int(out.get("revision") or 1),
        created_by=str(out.get("created_by") or ingest_path),
        model_version=str(out.get("model_version") or versions["model_version"]),
        embed_model_version=str(out.get("embed_model_version") or versions["embed_model_version"]),
        supersedes=str(out.get("supersedes") or ""),
    )
    return out
