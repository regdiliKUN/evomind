"""EvoMind 自主进化引擎。

一次学习任务的闭环：

    ① 诊断知识缺口  → ② 生成检索式 → ③ 多源检索 → ④ 抓取正文
    → ⑤ 提炼知识点 → ⑥ 去重入库 → ⑦ 资源发现 → ⑧ 反思下一轮

关键在于 ① 和 ⑧：不是把关键词搜一遍就完事，而是每轮都基于
「我已经知道什么」重新计算「我还缺什么」，所以越学越深、不原地打转。
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import config, downloader, fetcher, llm, memory, search

Listener = Callable[[dict], None]


# --------------------------------------------------------------------------
# 事件总线（前端 SSE 订阅）
# --------------------------------------------------------------------------
class EventBus:
    def __init__(self) -> None:
        self._subs: list[Listener] = []
        self._lock = threading.Lock()
        self.history: list[dict] = []

    def subscribe(self, fn: Listener) -> None:
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def publish(self, evt: dict) -> None:
        evt.setdefault("ts", time.time())
        with self._lock:
            self.history.append(evt)
            if len(self.history) > 800:
                self.history = self.history[-500:]
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(evt)
            except Exception:
                pass


bus = EventBus()


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
PLAN_SYSTEM = """你是 EvoMind 的学习规划器。你的任务不是复述已知，而是精准定位「知识缺口」并设计检索式。

规则：
1. 先看「已掌握知识」，任何已被覆盖的内容都不要再搜。
2. 缺口要具体：从「概念定义」推进到「机制原理 / 量化数据 / 边界条件 / 工程实践 / 最新进展 / 争议点」。
3. 每轮必须比上一轮更深一层，禁止与「历史检索式」重复。
4. **queries 里的每条检索式必须用英文书写**（专有名词保持原样，如 ZBrush / DynaMesh / SLA）。
   所有高质量检索源（arXiv、GitHub、StackExchange、HuggingFace）都是英文索引，
   中文检索式命中率极低，只会拿回产品页和安装教程。
   正确：「ZBrush DynaMesh polygon count tradeoff」，错误：「ZBrush 面数取舍怎么选」。
5. gaps 和 focus 用中文写（给人看），queries 用英文写（给搜索引擎看）。
6. 避开产品介绍、安装教程、购买链接这类无信息量的检索角度。
7. 优先能搜到「具体参数和判据」的角度，比如 minimum wall thickness、layer height limit、
   shrinkage compensation、polygon budget、resolution limit。"""

PLAN_USER = """## 学习方向
{direction}

## 已掌握知识（{n_known} 条，以下为抽样）
{known}

## 已用过的检索式（禁止重复）
{used}

## 当前是第 {round_no} / {total_rounds} 轮
{stage_hint}

请输出 JSON：
{{
  "gaps": ["缺口1", "缺口2", "缺口3"],
  "queries": ["检索式1", "检索式2", "检索式3", "检索式4"],
  "focus": "本轮一句话重点"
}}
queries 数量 {qnum} 个，必须彼此角度不同。"""

EXTRACT_SYSTEM = """你是知识提炼器。从网页正文中抽取与目标方向相关、可独立成立的知识点。

要求：
- claim：一句话结论，中文，具体可验证。禁止「很重要」「需要重视」这类空话。
- detail：2-4 句补充，含关键数字、条件、机制。
- 只提炼正文里真实出现的内容，绝不编造。
- 与目标方向无关的内容一律丢弃，返回空数组。
- 营销文、导流页、纯目录页 → 返回空数组。"""

EXTRACT_USER = """## 目标方向
{direction}

## 本轮重点
{focus}

## 来源
标题：{title}
链接：{url}
类型：{kind}

## 正文
{text}

输出 JSON：
{{"points": [{{"claim": "...", "detail": "...", "tags": ["..."], "relevance": 0.0, "confidence": 0.0}}]}}
最多 6 条，按 relevance 从高到低。"""

REFLECT_SYSTEM = """你是学习复盘器。基于本轮结果，判断下一步该往哪里深挖。
不要泛泛而谈，要给出可以直接变成搜索词的具体方向。"""

REFLECT_USER = """## 学习方向
{direction}

## 本轮新增知识点
{new_points}

## 本轮没解决的缺口
{gaps}

请输出 JSON：
{{"learned": "本轮一句话总结", "next": ["下一步该深挖的子主题1", "子主题2"], "quality": 0.0}}
quality 是本轮知识质量自评（0-1）。"""


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
class Engine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self.state: dict[str, Any] = {
            "running": False,
            "direction": "",
            "session_id": "",
            "round": 0,
            "total_rounds": 0,
            "stage": "idle",
            "points": 0,
            "fetched": 0,
            "focus": "",
            "started_at": 0,
        }
        self._used_queries: list[str] = []
        self._visited: set[str] = set()
        self._gaps: list[str] = []
        self._sources: list[str] | None = None

    # ---------------- 对外接口 ----------------
    def start(self, direction: str, rounds: int | None = None) -> dict:
        with self._start_lock:
            return self._start_locked(direction, rounds)

    def _start_locked(self, direction: str, rounds: int | None) -> dict:
        if self.state["running"]:
            return {"ok": False, "msg": "已有学习任务在跑，请先停止"}
        # 方向会被当作主题分组用，多行粘贴进来的换行和缩进要压平，
        # 否则同一个方向会分裂成两个主题。
        direction = " ".join(direction.split())
        if not direction:
            return {"ok": False, "msg": "请填写学习方向"}
        if rounds is None:
            rounds = config.get("max_rounds", 8)
        if type(rounds) is not int or not 1 <= rounds <= 100:
            return {"ok": False, "msg": "学习轮数必须是 1 到 100 的整数"}
        self._stop.clear()
        self._used_queries = []
        self._visited = set()
        self._gaps = []
        # 按「方向」判断要不要查 arXiv。软件操作、工艺参数这类方向查 arXiv
        # 只会拿回同名但不同义的论文（比如 ZBrush 的 DynaMesh vs 学术的 DynaMesh）。
        if search.should_use_arxiv(direction):
            self._sources = None  # 用默认全套
        else:
            self._sources = [s for s in search.DEFAULT_SOURCES if s != "arxiv"]
        sid = memory.start_session(direction)
        self.state.update(
            running=True, direction=direction, session_id=sid, round=0,
            total_rounds=rounds, stage="starting", points=0, fetched=0,
            focus="", started_at=time.time(),
        )
        self._thread = threading.Thread(
            target=self._run, args=(direction, rounds, sid), daemon=True
        )
        try:
            self._thread.start()
        except Exception:
            self.state.update(running=False, stage="idle")
            memory.update_session(sid, status="error", ended_at=time.time())
            raise
        return {"ok": True, "session_id": sid}

    def stop(self) -> dict:
        self._stop.set()
        self.state["stage"] = "stopping"
        return {"ok": True}

    def snapshot(self) -> dict:
        s = dict(self.state)
        s["elapsed"] = round(time.time() - s["started_at"]) if s["started_at"] else 0
        return s

    # ---------------- 事件 ----------------
    def _emit(self, level: str, msg: str, **payload) -> None:
        evt = {"level": level, "msg": msg, **payload}
        bus.publish(evt)
        # stage 也要落库：只落 ok/warn/error 的话，刷新页面就只剩「开始/完成」两条，
        # 中间「检索到什么、读了哪几篇、提炼出几条」全部丢失，用户没法排查为什么 0 条。
        if level in ("ok", "warn", "error", "stage"):
            memory.log_event(level, msg, payload)

    # ---------------- 主循环 ----------------
    def _run(self, direction: str, rounds: int, sid: str) -> None:
        t0 = time.time()
        try:
            self._emit("info", f"开始学习「{direction}」，计划 {rounds} 轮", stage="init")
            # 用户点了「开始学习」就是要一个确定答案：这里必须同步探测一次，
            # 不能拿轮询用的缓存态（healthy()）去判——服务刚启动时它还是 False，
            # 会把明明可用的 Ollama 误判成未运行。这一次阻塞是用户主动触发的。
            if not llm.tags(fresh=True)[0]:
                raise llm.LLMError("Ollama 未运行，无法开始学习")
            llm.ensure_models(lambda m: self._emit("info", m))

            for r in range(1, rounds + 1):
                if self._stop.is_set():
                    self._emit("warn", "收到停止指令，正在收尾")
                    break
                self.state["round"] = r
                self._round(direction, r, rounds, sid)
                memory.update_session(
                    sid, rounds=r, new_points=self.state["points"],
                    fetched=self.state["fetched"],
                )

            elapsed = round(time.time() - t0)
            memory.update_session(
                sid, status="done" if not self._stop.is_set() else "stopped",
                ended_at=time.time(), rounds=self.state["round"],
                new_points=self.state["points"], fetched=self.state["fetched"],
            )
            self._emit(
                "ok",
                f"学习完成：{self.state['round']} 轮，新增 {self.state['points']} 条知识，"
                f"阅读 {self.state['fetched']} 篇资料，耗时 {elapsed // 60} 分 {elapsed % 60} 秒",
                stage="done",
            )
        except Exception as e:  # noqa: BLE001
            self._emit("error", f"学习中断：{e}")
            memory.log_event("error", str(e), {"trace": traceback.format_exc()[:2000]})
            memory.update_session(sid, status="error", ended_at=time.time())
        finally:
            self.state.update(running=False, stage="idle")

    # ---------------- 单轮 ----------------
    def _round(self, direction: str, r: int, total: int, sid: str) -> None:
        # ① 诊断缺口
        self.state["stage"] = "planning"
        self._emit("stage", f"第 {r}/{total} 轮 · 诊断知识缺口", round=r)
        plan = self._plan(direction, r, total)
        queries = [q for q in (plan.get("queries") or []) if q.strip()][
            : int(config.get("queries_per_round", 4))
        ]
        self._gaps = plan.get("gaps") or []
        self.state["focus"] = plan.get("focus", "")
        if not queries:
            self._emit("warn", "本轮没有生成有效检索式，跳过")
            return
        self._emit(
            "plan",
            f"本轮重点：{self.state['focus'] or '—'}｜检索式 {len(queries)} 条",
            queries=queries, gaps=self._gaps,
        )
        self._used_queries.extend(queries)

        # ② 检索
        self.state["stage"] = "searching"
        self._emit("stage", f"第 {r}/{total} 轮 · 多源检索")
        results = self._search_all(queries)
        self._emit("info", f"检索到 {len(results)} 条候选来源")

        # ③ 抓取
        todo = self._pick_targets(results)
        if not todo:
            # 候选都读过了，直接 return 等于这一轮白跑。换一组更具体的检索词再试一次。
            direction = self.state.get("direction", "")
            extra = [
                f"{direction} 常见问题",
                f"{direction} 具体参数 经验",
            ]
            self._emit("info", "候选来源都读过了，换一组更具体的检索词再试")
            results2 = self._search_all(extra)
            todo = self._pick_targets(results2)
            if not todo:
                self._emit(
                    "warn",
                    "换检索词后仍没有可读的新资料。这个方向可能已经学透，"
                    "建议换一个更具体的方向，或去书库导入相关书籍",
                )
                return
        self.state["stage"] = "reading"
        self._emit("stage", f"第 {r}/{total} 轮 · 阅读 {len(todo)} 篇资料")
        docs = self._fetch_all(todo)
        ok_docs = [d for d in docs if d.get("ok")]
        self.state["fetched"] += len(ok_docs)
        self._emit(
            "info", f"成功读取 {len(ok_docs)}/{len(todo)} 篇",
            urls=[d["url"] for d in ok_docs[:5]],
        )

        # ④ 提炼入库
        self.state["stage"] = "extracting"
        self._emit("stage", f"第 {r}/{total} 轮 · 提炼知识点")
        new_points = self._extract_all(ok_docs, direction, sid)
        self.state["points"] += len(new_points)
        self._emit(
            "learn", f"本轮入库 {len(new_points)} 条新知识",
            points=[p["claim"] for p in new_points[:6]],
        )

        # ⑤ 资源发现（每 2 轮一次，避免太频繁）
        if config.get("auto_download", True) and r % 2 == 1:
            self.state["stage"] = "resource"
            self._emit("stage", f"第 {r}/{total} 轮 · 扫描可下载资源")
            try:
                got = downloader.discover_for_direction(
                    direction, on_event=lambda lv, m, p: self._emit(lv, m, **p)
                )
                if got:
                    self._emit("ok", f"本轮自主获取 {len(got)} 个外部资源")
            except Exception as e:  # noqa: BLE001
                self._emit("warn", f"资源扫描失败：{e}")

        # ⑥ 反思
        self.state["stage"] = "reflecting"
        refl = self._reflect(direction, new_points)
        self._emit(
            "reflect",
            f"{refl.get('learned', '本轮完成')}｜下一步：{'、'.join(refl.get('next') or []) or '继续扩展'}",
            quality=refl.get("quality"),
        )
        # 把反思结论直接变成下一轮的优先缺口
        self._gaps = list(refl.get("next") or []) + self._gaps

    # ---------------- 各步骤实现 ----------------
    def _plan(self, direction: str, r: int, total: int) -> dict:
        known = memory.search(direction, k=18)
        known_txt = (
            "\n".join(f"- {p['claim'][:90]}" for p in known) if known else "（暂无，这是第一轮）"
        )
        used_txt = "\n".join(f"- {q}" for q in self._used_queries[-20:]) or "（无）"
        stage_hint = (
            "这是起步阶段，优先建立全景框架和核心概念。"
            if r <= 2
            else "已有基础，进入机制原理与量化细节。"
            if r <= total * 0.6
            else "已较充分，转向边界条件、工程实践、争议与最新进展。"
        )
        try:
            return llm.chat_json(
                PLAN_SYSTEM,
                PLAN_USER.format(
                    direction=direction,
                    n_known=len(known),
                    known=known_txt,
                    used=used_txt,
                    round_no=r,
                    total_rounds=total,
                    stage_hint=stage_hint,
                    qnum=int(config.get("queries_per_round", 4)),
                ),
            )
        except Exception as e:  # noqa: BLE001
            self._emit("warn", f"规划失败，退回默认检索：{e}")
            return {
                "gaps": [direction],
                "queries": [
                    f"{direction} 核心原理",
                    f"{direction} latest research 2026",
                    f"{direction} 实践方法 教程",
                    f"{direction} benchmark 数据",
                ],
                "focus": direction,
            }

    def _search_all(self, queries: list[str]) -> list[dict]:
        all_results: list[dict] = []
        srcs = self._sources

        def one(q: str) -> list[dict]:
            try:
                return search.search(q, limit=10, sources=srcs)
            except Exception:
                return []

        self._emit("info", "检索式：" + " ｜ ".join(queries))

        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as ex:
            for res in ex.map(one, queries):
                all_results.extend(res)

        # 把各源命中数打出来，检索源失效时能一眼看出来
        from collections import Counter

        cnt = Counter(r.get("source", "?") for r in all_results)
        if cnt:
            detail = " ".join(f"{k}×{v}" for k, v in cnt.most_common())
            self._emit("info", f"各源命中：{detail}")
        else:
            self._emit("warn", "所有检索源都返回空（大概率被限流），本轮没有新资料")

        return search.dedupe(all_results)

    def _pick_targets(self, results: list[dict]) -> list[dict]:
        """先做基础过滤，再让模型从候选里挑真正值得读的。

        没有这道预筛，检索到的产品页、安装教程、新闻会把抓取配额占满，
        提炼阶段全是无关内容，最后一条知识都入不了库。
        """
        limit = int(config.get("fetch_per_round", 8))
        candidates: list[dict] = []
        for r in results:
            url = r.get("url", "")
            if not url or url in self._visited or memory.doc_seen(url):
                continue
            if any(x in url for x in (".pdf", ".zip", ".png", ".jpg", ".jpeg",
                                      "/login", "/signup", "/cart", "/pricing")):
                continue
            candidates.append(r)

        if not candidates:
            return []
        if len(candidates) <= 3:
            return candidates[:limit]

        # 候选多于配额时，用模型判断哪些真的对得上
        listing = []
        for i, r in enumerate(candidates[:24], 1):
            title = (r.get("title") or "")[:80]
            snip = (r.get("snippet") or "")[:110]
            listing.append(f"{i}. [{r.get('source','')}] {title} | {snip}")

        try:
            verdict = llm.chat_json(
                "你是资料筛选器。判断候选资料是否真的有助于理解给定学习方向。\n"
                "优先保留：讲原理、给具体参数/数值、有判据或对比的内容。\n"
                "坚决排除：产品介绍页、购买/下载页、安装教程、新闻通稿、"
                "纯目录页、与方向只是关键词巧合命中的内容。\n"
                "如果候选中没有完美的，也要挑出相对最接近的 2-3 条 —— "
                "返回空列表意味着这一轮完全白跑，比读到一般的内容更糟。\n"
                '输出 JSON: {"pick": [编号, ...], "why": "一句话"}',
                f"## 学习方向\n{self.state.get('direction','')}\n\n"
                f"## 本轮重点\n{self.state.get('focus','')}\n\n"
                f"## 候选资料\n" + "\n".join(listing) +
                f"\n\n挑出最多 {limit} 条最值得读的，按有用程度排序。"
                "只要与方向沾边就给个机会，完全不沾边的才排除。",
                temperature=0.1,
            )
            idxs = verdict.get("pick") or []
            picked = []
            for i in idxs:
                try:
                    n = int(i)
                except Exception:
                    continue
                if 1 <= n <= len(candidates):
                    item = candidates[n - 1]
                    if item not in picked:
                        picked.append(item)
                if len(picked) >= limit:
                    break
            if picked:
                self._emit(
                    "info",
                    f"预筛：{len(candidates)} 条候选 → 选出 {len(picked)} 条值得读的",
                    picked=[p.get("title", "")[:60] for p in picked[:4]],
                )
                return picked
            # 模型全拒：保底读 2 条。0 篇必然 0 条，2 篇至少还有机会提炼出东西，
            # 成本也就多 30 秒左右。
            fallback = candidates[:2]
            self._emit(
                "warn",
                f"预筛认为 {len(candidates)} 条都不够相关，保底读前 {len(fallback)} 条试试",
            )
            return fallback
        except Exception as e:  # noqa: BLE001
            self._emit("warn", f"预筛失败（{str(e)[:60]}），按原顺序取前 {limit} 条")

        # 预筛失败就退回原始顺序
        return candidates[:limit]

    def _fetch_all(self, targets: list[dict]) -> list[dict]:
        def one(t: dict) -> dict:
            url = t["url"]
            self._visited.add(url)
            try:
                doc = fetcher.fetch(url)
            except Exception as e:  # noqa: BLE001
                doc = {"ok": False, "url": url, "error": str(e)[:120], "title": "", "text": ""}
            doc["kind"] = t.get("kind", "web")
            doc["snippet"] = t.get("snippet", "")
            memory.mark_doc(url, doc.get("title", ""), "", len(doc.get("text", "")), doc.get("ok", False))
            return doc

        with ThreadPoolExecutor(max_workers=4) as ex:
            return list(ex.map(one, targets))

    def _extract_all(self, docs: list[dict], direction: str, sid: str) -> list[dict]:
        new_points: list[dict] = []
        focus = self.state.get("focus", "")
        min_rel = float(config.get("min_relevance", 0.45))

        def one(doc: dict) -> list[dict]:
            text = (doc.get("text") or "")[:9000]
            if len(text) < 200:
                return []
            # 先用摘要片段做相关性粗筛，省一次大模型调用
            if doc.get("kind") == "repo" and len(text) < 300:
                return []
            try:
                data = llm.chat_json(
                    EXTRACT_SYSTEM,
                    EXTRACT_USER.format(
                        direction=direction, focus=focus,
                        title=doc.get("title", "")[:200], url=doc.get("url", ""),
                        kind=doc.get("kind", "web"), text=text,
                    ),
                    temperature=0.2,
                )
            except Exception:
                return []
            out = []
            for p in (data.get("points") or [])[:6]:
                claim = (p.get("claim") or "").strip()
                if len(claim) < 12:
                    continue
                try:
                    rel = float(p.get("relevance", 0.5))
                    conf = float(p.get("confidence", 0.6))
                except Exception:
                    rel, conf = 0.5, 0.6
                if rel < min_rel:
                    continue
                out.append(
                    {
                        "claim": claim,
                        "detail": (p.get("detail") or "").strip()[:1200],
                        "tags": p.get("tags") or [],
                        "confidence": max(0.1, min(1.0, conf)),
                        "source_url": doc.get("url", ""),
                        "source_title": doc.get("title", ""),
                        "source_kind": doc.get("kind", "web"),
                    }
                )
            return out

        # 提炼并发度控制在 2，避免压垮本地大模型
        all_cands: list[dict] = []
        with ThreadPoolExecutor(max_workers=2) as ex:
            for pts in ex.map(one, docs):
                for p in pts:
                    p["topic"] = direction  # 批量入库按点读 topic，需显式带上
                    all_cands.append(p)
        candidate = len(all_cands)
        if not all_cands:
            return new_points

        # 一次性批量入库：替代原来「逐条 add_point」的 N 次全库矩阵乘 + N 次 commit
        # （一轮从若干篇里抽出几十条，逐条就是几十次矩阵乘 + 几十次提交，随库线性变慢）。
        # 等价性：去重阈值、向量构造、顺序接受语义都与逐条 add_point 一致（见第四轮 bench）。
        try:
            res = memory.add_points_batch(all_cands, session_id=sid)
        except Exception as e:  # noqa: BLE001
            self._emit("warn", f"批量入库失败：{str(e)[:80]}")
            return new_points

        new_points = res.get("accepted_points") or []
        for p in new_points:
            self._emit(
                "learn", f"＋ {p['claim'][:70]}",
                source=p.get("source_url", ""), topic=direction,
            )
        dup = int(res.get("dup", 0))

        # 0 条新知识时最容易被误认为「坏了」，把原因讲清楚
        if candidate and not new_points:
            self._emit(
                "info",
                f"本轮提炼出 {candidate} 条候选，全部与已有知识重复（去重阈值 "
                f"{config.get('dedup_threshold')}），没有新增。"
                "这个方向已经学得比较透了，换个角度或更具体的问题会更有收获",
            )
        elif dup:
            self._emit("info", f"候选 {candidate} 条，新增 {len(new_points)} 条，重复跳过 {dup} 条")
        return new_points

    def _reflect(self, direction: str, new_points: list[dict]) -> dict:
        if not new_points:
            return {"learned": "本轮未获得有效新知识", "next": [], "quality": 0}
        txt = "\n".join(f"- {p['claim'][:90]}" for p in new_points[:12])
        try:
            return llm.chat_json(
                REFLECT_SYSTEM,
                REFLECT_USER.format(
                    direction=direction, new_points=txt,
                    gaps="\n".join(f"- {g}" for g in self._gaps[:6]) or "（无）",
                ),
                temperature=0.3,
            )
        except Exception:
            return {"learned": f"本轮新增 {len(new_points)} 条", "next": [], "quality": 0.5}


# --------------------------------------------------------------------------
# 基于知识库的问答（学习成果的直接体现）
# --------------------------------------------------------------------------
def answer_with_knowledge(question: str, topic: str | None = None) -> dict:
    hits = memory.search(question, k=8, topic=topic)
    if not hits:
        hits = memory.keyword_search(question, k=6)
    if not hits:
        return {
            "answer": "知识库里还没有相关内容。建议先为这个方向启动一轮学习，我学会之后再回答你。",
            "sources": [],
            "hits": 0,
        }
    ctx = "\n\n".join(
        f"[{i+1}] {h['claim']}\n{h.get('detail','')}\n出处：{h.get('source_title','')} {h.get('source_url','')}"
        for i, h in enumerate(hits)
    )
    sys = (
        "你是 EvoMind，一个靠自学成长起来的助手。回答必须基于给定的知识片段。\n"
        "规则：\n"
        "1. 只使用片段中的信息，不要补充片段以外的臆测。\n"
        "2. 用 [1] [2] 标注引用编号。\n"
        "3. 片段不足以回答时，明确说「我目前学到的还不够回答这部分」，并指出缺什么。\n"
        "4. 中文回答，结构清晰，能用要点就用要点。"
    )
    ans = llm.chat(
        [{"role": "system", "content": sys},
         {"role": "user", "content": f"知识片段：\n{ctx}\n\n问题：{question}"}],
        temperature=0.3,
    )
    return {
        "answer": ans,
        "sources": [
            {"title": h.get("source_title"), "url": h.get("source_url"), "score": h["score"]}
            for h in hits
        ],
        "hits": len(hits),
    }


engine = Engine()
