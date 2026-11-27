"""关键词知识总结：把「学到的、与某个概念相关的全部知识」提炼成一篇结构化总结。

为什么不是直接 memory.search + 一次 LLM：
实测像「负形」这种短概念词，库里往往没有逐字命中，而 bge-m3 的语义分数又很平
（相关与不相关都挤在 0.50~0.60），混进大量「负值参数」「负向雕刻」之类的字面近亲。
单靠向量阈值要么漏、要么脏。所以走四步流水线：

1. 扩展：让模型给出同义词 / 中英文术语 / 紧密相关的说法（负形 → 负空间、
   negative space、图底关系……），失败就只用原词；
2. 召回：每个词做「字面 LIKE 命中 + 语义检索」，按知识状态过滤（默认只要
   status='verified' 的已证实知识），去重后取前 max_points 条候选；
3. 筛选：候选按批交给模型判断「是否真的在讲这个概念」，剔除字面近亲；
   某批模型调用失败时只保留该批的字面命中，绝不把未筛的噪声当成相关；
4. 汇总：相关条目带编号喂给模型写成结构化 Markdown，每个论断标注 [n]；
   条目太多时先分组小结再合并（map-reduce），避免超出上下文。

结果存进 knowledge_digests 表，可回看、导出 Markdown、删除。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Callable, Iterator

from . import evidence, llm, memory

#: 一次只跑一个总结：本地大模型串行更快，也避免和自己抢显存
_run_lock = threading.Lock()

SCREEN_BATCH = 25          # 每批交给模型筛选的条目数
MAP_GROUP = 45             # 超过这个数就分组小结再合并
DETAIL_CHARS_SCREEN = 160  # 筛选时每条附带的补充说明长度
DETAIL_CHARS_SUM = 320     # 汇总时每条附带的补充说明长度
SEMANTIC_PER_TERM = 40     # 每个扩展词保留的语义命中数（过滤状态之后）
MAX_TERMS = 6

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_digests (
    id TEXT PRIMARY KEY,
    keyword TEXT NOT NULL,
    topic TEXT DEFAULT '',
    include_legacy INTEGER DEFAULT 0,
    terms TEXT DEFAULT '[]',
    point_ids TEXT DEFAULT '[]',
    summary TEXT DEFAULT '',
    stats TEXT DEFAULT '{}',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_digest_created ON knowledge_digests(created_at);
"""
def _ensure_table() -> None:
    # 不做进程级「已建表」标记：备份恢复 / 测试换库后标记会失真，
    # CREATE IF NOT EXISTS 本身足够便宜。
    c = memory.conn()
    c.executescript(_DDL)
    c.commit()


def _status_filter(include_legacy: bool) -> str:
    """默认只取已证实知识；勾选后放宽到所有在用状态（含历史未复核的 legacy）。"""
    if include_legacy:
        return evidence.ACTIVE_FILTER
    return "COALESCE(status,'legacy')='verified'"


def _status_ok(status: str | None, include_legacy: bool) -> bool:
    s = status or "legacy"
    if include_legacy:
        return s not in ("archived", "disputed", "superseded")
    return s == "verified"


# --------------------------------------------------------------------------
# 1. 扩展
# --------------------------------------------------------------------------
def expand_terms(keyword: str) -> tuple[list[str], str]:
    """返回 (检索词列表, 概念的一句话释义)。原词永远排第一。"""
    keyword = keyword.strip()
    terms = [keyword]
    meaning = ""
    try:
        got = llm.chat_json(
            "你是术语专家。给定一个概念关键词，列出与它指同一概念或紧密相关的检索词："
            "同义词、中英文对应术语、学科内的常见别称。不要列出只是字面相似但含义不同的词"
            "（例如「负形」是造型/构图里的负空间，不是数学里的负数）。"
            '只输出 JSON：{"meaning":"一句话说明该概念","terms":["词1","词2"]}，terms 最多 6 个。',
            f"关键词：{keyword}",
            retries=1, temperature=0.2, timeout=120,
        )
        extra = got.get("terms") if isinstance(got, dict) else None
        meaning = str(got.get("meaning") or "")[:120] if isinstance(got, dict) else ""
        for t in extra or []:
            t = str(t).strip()
            if 1 < len(t) <= 40 and t.lower() not in {x.lower() for x in terms}:
                terms.append(t)
    except Exception as e:  # noqa: BLE001  扩展只是增强，失败就只用原词
        memory.log_event("warn", f"知识总结：关键词扩展失败，仅用原词（{type(e).__name__}）")
    return terms[: MAX_TERMS + 1], meaning


# --------------------------------------------------------------------------
# 2. 召回
# --------------------------------------------------------------------------
def _shape(row: Any) -> dict:
    d = dict(row)
    d.pop("embedding", None)
    d.pop("_rid", None)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]") if isinstance(d.get("tags"), str) else d.get("tags") or []
    except Exception:  # noqa: BLE001
        d["tags"] = []
    return d


def literal_hits(term: str, *, topic: str | None, include_legacy: bool, limit: int = 200) -> list[dict]:
    """claim / detail / topic / tags 里逐字出现该词的知识。单字词太宽，不做字面匹配。"""
    term = term.strip()
    if len(term) < 2:
        return []
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = (
        "SELECT * FROM knowledge WHERE (claim LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' "
        "OR topic LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\') AND " + _status_filter(include_legacy)
    )
    params: list[Any] = [like, like, like, like]
    if topic:
        sql += " AND topic=?"
        params.append(topic)
    sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
    params.append(int(limit))
    return [_shape(r) for r in memory.conn().execute(sql, params).fetchall()]


def collect(
    keyword: str, terms: list[str], *, topic: str | None = None,
    include_legacy: bool = False, max_points: int = 120,
) -> dict:
    """字面命中优先（score 记为 1+），语义命中按相似度补足，去重后截取前 max_points 条。"""
    pool: dict[str, dict] = {}
    n_literal = n_semantic = 0
    for t in terms:
        for d in literal_hits(t, topic=topic, include_legacy=include_legacy):
            old = pool.get(d["id"])
            if old is None or not old.get("_literal"):
                n_literal += old is None
                d["_literal"] = True
                d["_score"] = 1.0 + (0.1 if t == keyword else 0.0)
                d["_via"] = t
                pool[d["id"]] = d
    for t in terms:
        try:
            # 已证实知识只占库的一部分，先放宽 k 再按状态过滤，保证每个词都能拿到足量命中
            hits = memory.search(t, k=SEMANTIC_PER_TERM * (1 if include_legacy else 4), topic=topic)
        except Exception:  # noqa: BLE001
            hits = []
        kept = [h for h in hits if _status_ok(h.get("status"), include_legacy)][:SEMANTIC_PER_TERM]
        for h in kept:
            s = float(h.get("score") or 0.0)
            old = pool.get(h["id"])
            if old is None:
                n_semantic += 1
                h = _shape(h)
                h["_literal"] = False
                h["_score"] = s
                h["_via"] = t
                pool[h["id"]] = h
            elif not old.get("_literal") and s > old["_score"]:
                old["_score"] = s
    ranked = sorted(pool.values(), key=lambda d: -d["_score"])
    return {
        "items": ranked[: max(1, int(max_points))],
        "literal": n_literal, "semantic": n_semantic, "pool": len(ranked),
    }


# --------------------------------------------------------------------------
# 3. 筛选
# --------------------------------------------------------------------------
def _screen_batch(keyword: str, meaning: str, batch: list[dict]) -> list[int] | None:
    lines = "\n".join(
        f"[{i}] {d['claim']}" + (f"｜{(d.get('detail') or '')[:DETAIL_CHARS_SCREEN]}" if d.get("detail") else "")
        for i, d in enumerate(batch)
    )
    try:
        got = llm.chat_json(
            "你在为一个概念整理知识。判断下面每条知识是否真正在讲这个概念"
            "（它的定义、原理、用法、技巧、例子、与其他概念的关系）。"
            "只是字面上含有相同的字、实际讲的是别的东西的，一律排除；只是顺带一提、"
            "对理解该概念没有帮助的也排除。"
            '只输出 JSON：{"relevant":[相关条目的编号]}，没有就输出空数组。',
            f"概念：{keyword}" + (f"（{meaning}）" if meaning else "") + f"\n\n候选知识：\n{lines}",
            retries=1, temperature=0.0, timeout=300,
        )
    except Exception:  # noqa: BLE001
        return None
    ids = got.get("relevant") if isinstance(got, dict) else None
    if not isinstance(ids, list):
        return None
    out = []
    for x in ids:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(batch) and i not in out:
            out.append(i)
    return out


# --------------------------------------------------------------------------
# 4. 汇总
# --------------------------------------------------------------------------
_SUM_RULES = (
    "规则：\n"
    "1. 只使用给出的知识条目，不补充条目以外的臆测；条目之间矛盾时如实并列指出。\n"
    "2. 每个要点后用 [n] 标注依据的条目编号，可多个，如 [3][7]。\n"
    "3. 中文，Markdown 格式，层次清晰，能用要点就用要点，避免空话套话。\n"
    "4. 合并重复的说法，按主题归类，而不是逐条复述。"
)
_SUM_LAYOUT = (
    "按下面的结构输出（某节没有内容就省略该节）：\n"
    "## 一句话概括\n## 核心概念与原理\n## 关键要点\n## 实践方法与技巧\n"
    "## 注意事项与常见误区\n## 与其他概念的关系\n## 知识缺口（目前学到的还缺什么）"
)


def _ctx(points: list[dict], start: int = 1) -> str:
    out = []
    for n, d in enumerate(points, start):
        s = f"[{n}] {d['claim']}"
        if d.get("detail"):
            s += f"\n    {(d['detail'] or '')[:DETAIL_CHARS_SUM]}"
        if d.get("topic"):
            s += f"\n    （方向：{d['topic']}）"
        out.append(s)
    return "\n".join(out)


def summarize(keyword: str, points: list[dict], *,
              on_step: Callable[[int, int], None] | None = None) -> str:
    sys = f"你是 EvoMind，正在把自己学到的关于「{keyword}」的知识整理成一份学习总结。\n" + _SUM_RULES
    if len(points) <= MAP_GROUP:
        if on_step:
            on_step(1, 1)
        return llm.chat(
            [{"role": "system", "content": sys},
             {"role": "user", "content": f"{_SUM_LAYOUT}\n\n知识条目（共 {len(points)} 条）：\n{_ctx(points)}"}],
            temperature=0.3, timeout=900,
        ).strip()
    groups = [(i + 1, points[i:i + MAP_GROUP]) for i in range(0, len(points), MAP_GROUP)]
    total = len(groups) + 1
    partials = []
    for gi, (start, grp) in enumerate(groups):
        if on_step:
            on_step(gi + 1, total)
        partials.append(llm.chat(
            [{"role": "system", "content": sys},
             {"role": "user", "content":
              "先把这一组条目归纳成要点清单（保留原编号引用 [n]，不要写开头结尾）：\n"
              f"{_ctx(grp, start)}"}],
            temperature=0.3, timeout=900,
        ).strip())
    if on_step:
        on_step(total, total)
    merged = "\n\n".join(f"### 第 {i + 1} 组要点\n{p}" for i, p in enumerate(partials))
    return llm.chat(
        [{"role": "system", "content": sys},
         {"role": "user", "content":
          f"{_SUM_LAYOUT}\n\n下面是分组归纳出的要点（引用编号是全局编号，务必原样保留），"
          f"请合并去重、重新组织成一份完整总结：\n\n{merged}"}],
        temperature=0.3, timeout=900,
    ).strip()


# --------------------------------------------------------------------------
# 编排 + 存储
# --------------------------------------------------------------------------
def _brief(d: dict, n: int) -> dict:
    return {
        "n": n, "id": d["id"], "claim": d["claim"], "detail": (d.get("detail") or "")[:400],
        "topic": d.get("topic", ""), "status": d.get("status") or "legacy",
        "source_title": d.get("source_title", ""), "source_url": d.get("source_url", ""),
        "literal": bool(d.get("_literal")), "score": round(float(d.get("_score") or 0), 4),
        "via": d.get("_via", ""),
    }


def run(keyword: str, *, topic: str | None = None, include_legacy: bool = False,
        max_points: int = 120) -> Iterator[dict]:
    """流水线生成器：逐步产出进度事件，最后一条 stage='done' 携带完整记录。"""
    keyword = (keyword or "").strip()
    if not keyword:
        yield {"stage": "error", "msg": "请输入关键词"}
        return
    if not _run_lock.acquire(blocking=False):
        yield {"stage": "error", "msg": "已有一个知识总结在进行中，请等它完成后再试"}
        return
    try:
        _ensure_table()
        t0 = time.time()
        yield {"stage": "expand", "progress": .03, "msg": f"正在理解「{keyword}」并扩展同义检索词…"}
        terms, meaning = expand_terms(keyword)
        yield {"stage": "expand", "progress": .08, "terms": terms, "meaning": meaning,
               "msg": "检索词：" + "、".join(terms) + (f"（{meaning}）" if meaning else "")}

        scope = "已证实 + 历史未复核" if include_legacy else "仅已证实"
        yield {"stage": "collect", "progress": .1, "msg": f"在知识库中召回候选（{scope}）…"}
        got = collect(keyword, terms, topic=topic, include_legacy=include_legacy, max_points=max_points)
        cands = got["items"]
        yield {"stage": "collect", "progress": .15,
               "msg": f"召回 {got['pool']} 条（字面命中 {got['literal']}、语义命中 {got['semantic']}），"
                      f"取前 {len(cands)} 条进入筛选"}
        if not cands:
            # 「没找到」是正常结局而不是故障：单独用 stage=empty，前端据此把进度条收满并给出建议
            yield {"stage": "empty", "progress": 1.0,
                   "msg": f"知识库里还没有与「{keyword}」相关的{scope}知识。"
                                            "可以先为这个方向启动一轮学习，或勾选「包含历史未复核知识」再试。"}
            return

        yield {"stage": "screen", "progress": .16, "msg": "让模型逐批核对候选是否真的在讲这个概念…"}
        kept: list[dict] = []
        failed = 0
        batches = [cands[i:i + SCREEN_BATCH] for i in range(0, len(cands), SCREEN_BATCH)]
        for bi, batch in enumerate(batches):
            idx = _screen_batch(keyword, meaning, batch)
            if idx is None:
                # 模型这批没答上来：只留字面命中，绝不把未筛的语义噪声当成相关
                failed += 1
                kept.extend(d for d in batch if d.get("_literal"))
            else:
                kept.extend(batch[i] for i in idx)
            yield {"stage": "screen", "progress": .16 + .44 * (bi + 1) / len(batches),
                   "msg": f"逐批核对相关性：{bi + 1}/{len(batches)} 批，已确认相关 {len(kept)} 条"}
        kept.sort(key=lambda d: (-int(bool(d.get("_literal"))), -d["_score"]))
        if not kept:
            n_lit = sum(1 for d in cands if d.get("_literal"))
            yield {"stage": "empty", "progress": 1.0,
                   "msg": f"候选里没有真正在讲「{keyword}」的知识（共核对 {len(cands)} 条"
                          + (f"，其中 {n_lit} 条字面含检索词，但模型判断只是顺带提及" if n_lit else "")
                          + "）。"
                          + ("部分批次模型调用失败，可稍后重试。" if failed else
                             "可以先为这个概念启动一轮学习"
                             + ("" if include_legacy else "，或勾选「包含历史未复核知识」") + "再试。")}
            return

        steps: list[dict] = []

        def on_step(i: int, n: int) -> None:
            steps.append({"stage": "summarize", "progress": .62 + .35 * (i - 1) / n,
                          "msg": f"撰写总结：第 {i}/{n} 步（共 {len(kept)} 条相关知识）"})

        yield {"stage": "summarize", "progress": .62, "msg": f"基于 {len(kept)} 条相关知识撰写总结…"}
        # summarize 内部多次调用模型，进度通过线程把回调事件转出来
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["text"] = summarize(keyword, kept, on_step=on_step)
            except Exception as e:  # noqa: BLE001
                result["err"] = e

        th = threading.Thread(target=work, daemon=True, name="digest-summarize")
        th.start()
        while th.is_alive():
            th.join(1.0)
            while steps:
                yield steps.pop(0)
            if th.is_alive():
                yield {"stage": "tick", "elapsed": round(time.time() - t0, 1)}
        while steps:
            yield steps.pop(0)
        if "err" in result:
            yield {"stage": "error", "msg": f"撰写总结失败：{result['err']}"}
            return

        points = [_brief(d, n) for n, d in enumerate(kept, 1)]
        stats = {
            "candidates": len(cands), "pool": got["pool"], "literal": got["literal"],
            "semantic": got["semantic"], "relevant": len(kept), "failed_batches": failed,
            "seconds": round(time.time() - t0, 1),
            "topics": _topic_counts(kept),
        }
        rec = save(keyword=keyword, topic=topic or "", include_legacy=include_legacy,
                   terms=terms, points=points, summary=result["text"], stats=stats)
        memory.log_event("info", f"知识总结完成：「{keyword}」，{len(kept)} 条相关知识，用时 {stats['seconds']}s")
        yield {"stage": "done", "progress": 1.0, "msg": "总结完成", "digest": rec}
    finally:
        _run_lock.release()


def _topic_counts(items: list[dict]) -> list[dict]:
    cnt: dict[str, int] = {}
    for d in items:
        t = d.get("topic") or ""
        cnt[t] = cnt.get(t, 0) + 1
    return [{"topic": t, "n": n} for t, n in sorted(cnt.items(), key=lambda x: -x[1])[:12]]


def save(*, keyword: str, topic: str, include_legacy: bool, terms: list[str],
         points: list[dict], summary: str, stats: dict) -> dict:
    _ensure_table()
    did = uuid.uuid4().hex[:12]
    now = time.time()
    c = memory.conn()
    c.execute(
        "INSERT INTO knowledge_digests(id,keyword,topic,include_legacy,terms,point_ids,summary,stats,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (did, keyword, topic, int(include_legacy), json.dumps(terms, ensure_ascii=False),
         json.dumps(points, ensure_ascii=False), summary, json.dumps(stats, ensure_ascii=False), now),
    )
    c.commit()
    return get(did)  # type: ignore[return-value]


def _row(r: Any, full: bool) -> dict:
    d = dict(r)
    for k, dv in (("terms", []), ("point_ids", []), ("stats", {})):
        try:
            d[k] = json.loads(d.get(k) or json.dumps(dv))
        except Exception:  # noqa: BLE001
            d[k] = dv
    d["points"] = d.pop("point_ids")
    d["include_legacy"] = bool(d.get("include_legacy"))
    if not full:
        d["count"] = len(d.pop("points"))
        d["preview"] = re.sub(r"[#*\[\]\d]+", "", d.pop("summary") or "").strip()[:120]
    return d


def list_digests(limit: int = 50) -> list[dict]:
    _ensure_table()
    rows = memory.conn().execute(
        "SELECT * FROM knowledge_digests ORDER BY created_at DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [_row(r, full=False) for r in rows]


def get(did: str) -> dict | None:
    _ensure_table()
    r = memory.conn().execute("SELECT * FROM knowledge_digests WHERE id=?", (did,)).fetchone()
    return _row(r, full=True) if r else None


def delete(did: str) -> bool:
    _ensure_table()
    c = memory.conn()
    n = c.execute("DELETE FROM knowledge_digests WHERE id=?", (did,)).rowcount
    c.commit()
    return bool(n)


def to_markdown(d: dict) -> str:
    scope = "已证实 + 历史未复核知识" if d.get("include_legacy") else "仅已证实知识"
    st = d.get("stats") or {}
    head = (
        f"# 「{d['keyword']}」知识总结\n\n"
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(d.get('created_at') or 0))}"
        f" · 范围：{scope}" + (f" · 方向：{d['topic']}" if d.get("topic") else "")
        + f" · 相关知识 {st.get('relevant', len(d.get('points') or []))} 条"
        + f"\n> 检索词：{'、'.join(d.get('terms') or [])}\n\n"
    )
    refs = "\n".join(
        f"{p['n']}. {p['claim']}" + (f"（{p['source_title']} {p['source_url']}）".replace("（ ", "（")
                                    if p.get("source_title") or p.get("source_url") else "")
        for p in d.get("points") or []
    )
    return head + (d.get("summary") or "") + "\n\n---\n\n## 引用的知识条目\n\n" + refs + "\n"
