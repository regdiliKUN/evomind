"""学习方向自动更换（方向轮换）。

用户只给一个模糊方向（例如「光固化 3D 打印」），系统自己把这件事跑下去：

    拆解子方向 → 选定一个连续学 N 轮 → 换向之前校验语义边界
    → 从剩余子方向里随机挑下一个 → 用尽则补充合法子方向
    → 达到总轮次上限或用户停止 → 汇总各方向成果

两个设计要点：

① 边界校验单独成层。模型拆解时很容易滑向相邻领域（问 ZBrush 拆出 Blender
  拓扑、问 RAG 拆出微调），一旦落库就污染整个知识库的主题分布。所以每次
  换向都再核一次，越界的丢弃重选，而不是只靠拆解时的提示词约束。
② 轮次结算用引擎的 completed 而不是 round。round 是「正在跑第几轮」，
  中途停止会多算一轮；轮换按轮次换方向，虚报一轮就会让进度和配额全错。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import traceback
import uuid
from typing import Any

from . import config, directions, evolve, llm, memory, tasks

#: evolve.Engine.start 单次最多接受 100 轮，超出要分批交给引擎。
BATCH_MAX = 100
#: 子方向池最多扩到「首轮数量 × 该系数」，防止补充机制无限膨胀。
PLAN_CAP_FACTOR = 6
#: 单次换向最多尝试几个候选，避免模型持续给出越界内容时死循环。
PICK_TRIES = 8
#: 任务表保留多少条历史。界面只看最近 5 条，留 100 条够回溯，也不至于无限增长。
KEEP_RUNS = 100
#: 模型/网络不可用时的重试策略。Ollama 开机自启没就绪、重启中都属于「暂时」，
#: 直接暂停会让人以为任务死了；改为指数退避重试，超过上限才真正暂停。
MODEL_BACKOFF_BASE = 30
MODEL_BACKOFF_MAX = 300
MODEL_RETRY_LIMIT = 20

#: 运行中的阶段文案。以前运行中一律显示「进行中」，等待模型重试时看不出区别。
ACTIVE_STAGE_TEXT = {
    "splitting": "拆解子方向",
    "switching": "切换方向",
    "waiting_model": "等待模型",
    "pausing": "暂停中",
    "stopping": "结束中",
}

STATUS_TEXT = {
    # 键是 stop_reason：结束原因只有这几种，文案统一在这里，前端不再各写一份
    "total_rounds": "已完成",
    "stopped": "已手动结束",
    "error": "异常结束",
    "no_plan": "无可用子方向",
    "no_direction": "无可用子方向",
}

SPLIT_SYSTEM = """你是学习课程设计器。给定一个模糊的学习领域，把它拆成若干具体子方向。

硬性要求：
1. 每个子方向必须严格落在母方向的语义范围内。不要扩展到相邻领域、上位泛化主题或
   只是关键词字面相近的东西（例：母方向「ZBrush」不要出现 Blender、Maya、通用建模理论）。
2. 粒度要具体到「可检索、可验证」：写清对象 + 侧面（参数、机制、流程、判据、排错），
   不要写「基础入门」「进阶技巧」这种无法检索的空壳。
3. 子方向之间角度不重复，覆盖该领域的不同侧面。
4. 不要编号，不要加母方向名称前缀，每条 8-40 字。
只输出 JSON：{"items": ["子方向1", "子方向2"]}"""

SPLIT_USER = """## 母方向
{seed}

## 已有子方向（不要重复、不要近义改写）
{existing}

请再给出 {count} 个新的子方向。"""

JUDGE_SYSTEM = """你是学习范围审核员。判断「候选子方向」是否严格属于「母方向」的语义范围。

判定标准：
- 属于：讨论母方向内部的具体主题、参数、流程、原理或应用。
- 不属于：属于相邻领域、上位泛化主题、或只是关键词字面重合但实际讲别的东西。
拿不准时判为不属于。只输出 JSON：{"in_scope": true/false, "reason": "一句话"}"""

JUDGE_USER = """## 母方向
{seed}

## 候选子方向
{candidate}"""

JUDGE_MANY_SYSTEM = """你是学习范围审核员。下面给出一个母方向和若干候选子方向，请逐条判断候选是否严格属于母方向的语义范围。

判定标准：
- 属于：讨论母方向内部的具体主题、参数、流程、原理或应用。
- 不属于：属于相邻领域、上位泛化主题、或只是关键词字面重合但实际讲别的东西。
拿不准时判为不属于。每条都要给结论，不要漏。
只输出 JSON：{"verdicts": [{"index": 1, "in_scope": true, "reason": "一句话"}]}"""


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
_lock = threading.RLock()
_stop_evt = threading.Event()
_wake_evt = threading.Event()
_scope_cache: dict[tuple[str, str], tuple[bool, str]] = {}

#: 本进程启动时刻。用于区分「上次进程留下的僵尸任务」和「另一个进程正在跑的任务」。
BOOT_TS = time.time()

_state: dict[str, Any] = {
    "active": False,      # 任务存在（运行中或暂停中都算）
    "running": False,     # 有批次正在跑
    "paused": False,
    "in_batch": False,
    "stage": "idle",
    "seed": "",
    "run_id": "",
    "plan": [],           # [{"id","direction","seq","status","rounds","points",...}]
    "current": "",
    "current_id": "",
    "rounds_in_current": 0,
    "rounds_per_direction": 100,
    "total_round_limit": 300,
    "total_rounds_done": 0,
    "subdirection_count": 6,
    "switches": [],
    "summary": None,
    "error": "",
    "stop_reason": "",
    "blocked": "",        # 引擎被别的任务占用时的原因（等待中，不算失败）
    "skip_requested": False,  # 用户点了「跳过当前方向」，等这一批收尾后换向
    "retry_in": 0,        # 模型不可用时，还有多少秒重试（退避中）
    "preset": [],         # 预览过、用户确认过的子方向，启动时优先沿用
    "started_at": 0.0,
    "finished_at": 0.0,
    "failures": 0,
    "thread": None,
}


def _emit(level: str, msg: str, **payload) -> None:
    # rotation=True 让前端能把轮换事件和普通学习事件区分开（面板局部刷新）
    evolve.emit(level, msg, rotation=True, **payload)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


# --------------------------------------------------------------------------
# 子方向拆解
# --------------------------------------------------------------------------
def _dedupe_key(text: str) -> str:
    """去重用的紧凑键：忽略空白、标点与大小写。

    「DynaMesh 面数取舍」和「DynaMesh面数取舍」在检索层面是同一件事，
    按原字符串比对会漏掉，最后计划里出现两条几乎一样的方向。
    """
    return re.sub(r"[\s\W_]+", "", _norm(text))


def _strip_seed_prefix(text: str, seed: str) -> str:
    """去掉模型自己加上的母方向前缀。

    模型有时返回「ZBrush：DynaMesh 取舍」，本地兜底拼的是「ZBrush：…」，
    两者混在一起时，同一个方向会以「带前缀/不带前缀」两种形态各存一份。
    """
    for sep in ("：", ":", "—", "|", "、", "，", ",", " "):
        head = seed + sep
        if text.startswith(head):
            rest = text[len(head):].strip()
            if len(rest) >= 4:
                return rest
    return text


def _clean_items(raw: Any, seed: str, exclude: set[str]) -> list[str]:
    """把模型输出洗成干净的子方向列表：去编号、去母方向前缀、去重、去空壳。"""
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("directions") or []
    if not isinstance(raw, list):
        return []
    seed_key = _dedupe_key(seed)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            item = item.get("direction") or item.get("title") or ""
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        text = re.sub(r"^\s*(?:\d+[.、)]|[-*·])\s*", "", text).strip(" \"'“”「」")
        text = text[:120]
        # 去重键去掉母方向前缀（模型写的「ZBrush：X」和本地拼的「ZBrush：X」
        # 是同一个方向），但原文保留前缀——否则界面上看不出它属于哪个母方向，
        # 字面范围判断也会失效（本地拼装方向本来靠前缀保证不越界）。
        key = _dedupe_key(_strip_seed_prefix(text, seed))
        # 长度太短的（如「基础」）检索不出东西；与母方向同名的等于没拆
        if len(text) < 4 or key == seed_key or key in seen or key in exclude:
            continue
        seen.add(key)
        out.append(text)
    return out


def _split(seed: str, count: int, exclude: list[str] | None = None) -> tuple[list[str], str]:
    """拆出 count 个子方向，返回 (方向列表, 来源说明)。

    先问模型；模型不可用或数量不够时用本地候选池补齐 —— 本地拼出来的方向
    带着母方向前缀，天然在范围内，保证轮换不会因为模型抽风而开不了局。
    """
    exclude_keys = {_dedupe_key(s) for s in (exclude or [])}
    items: list[str] = []
    source = "model"
    try:
        data = llm.chat_json(
            SPLIT_SYSTEM,
            SPLIT_USER.format(
                seed=seed,
                existing="\n".join(f"- {s}" for s in (exclude or [])[:40]) or "（无）",
                count=count,
            ),
            temperature=0.5,
        )
        items = _clean_items(data, seed, exclude_keys)
    except Exception as e:  # noqa: BLE001
        _emit("warn", f"子方向拆解失败，改用本地候选池：{str(e)[:100]}")
        source = "local"

    if len(items) < count:
        # 本地池按母方向拼装，用作补齐：既保证数量，也保证不越界
        local = directions.compose(
            seed, directions.deck(seed), list(exclude or []) + items, count - len(items)
        )
        for text in local:
            key = _dedupe_key(_strip_seed_prefix(text, seed))
            if key in exclude_keys or key in {_dedupe_key(t) for t in items}:
                continue
            items.append(text)
        source = "model+local" if source == "model" else "local"
    return items[:count], source


# --------------------------------------------------------------------------
# 语义边界校验
# --------------------------------------------------------------------------
def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _tokens(text: str) -> set[str]:
    text = _norm(text)
    latin = set(re.findall(r"[a-z0-9][a-z0-9+._-]{1,}", text))
    cjk = {text[i:i + 2] for i in range(len(text) - 1) if _is_cjk(text[i]) and _is_cjk(text[i + 1])}
    return latin | cjk


#: 字面预判的重合阈值。取 0.85 而不是 0.6：0.6 会让「FDM 与光固化打印支撑结构差异」
#: 这种只命中部分关键词的相邻工艺直接通过，绕过模型审核。留下 0.85 后，
#: 只有「几乎全部关键词都在」的改写（如去掉空格的同义写法）才走快路径，
#: 其余一律交给审核，宁可多一次调用也不放跑相邻领域。
LEXICAL_OVERLAP = 0.85


def _lexical_in_scope(seed: str, candidate: str) -> bool:
    """便宜的包含判断：本地拼装的方向必然命中，模型给的方向通常要靠审核。"""
    s, c = _norm(seed), _norm(candidate)
    if not s or not c:
        return False
    if s in c:
        return True
    st = _tokens(s)
    return bool(st) and len(st & _tokens(c)) / len(st) >= LEXICAL_OVERLAP


def in_scope(seed: str, candidate: str) -> tuple[bool, str]:
    """候选方向是否仍在母方向的语义范围内。结果按 (母方向, 候选) 缓存。"""
    if not _norm(seed):
        return True, "母方向为空，跳过校验"
    key = (_norm(seed), _norm(candidate))
    with _lock:
        hit = _scope_cache.get(key)
    if hit is not None:
        return hit

    if _lexical_in_scope(seed, candidate):
        verdict = (True, "包含母方向关键词")
    else:
        try:
            data = llm.chat_json(
                JUDGE_SYSTEM,
                JUDGE_USER.format(seed=seed, candidate=candidate),
                temperature=0.0,
            )
            if not isinstance(data, dict) or not isinstance(data.get("in_scope"), bool):
                raise ValueError("审核输出缺少 in_scope 布尔值")
            verdict = (bool(data["in_scope"]), str(data.get("reason") or "")[:120])
        except Exception as e:  # noqa: BLE001
            # 审核不可用时不能把所有方向都判死：拆解本身已被同一母方向约束过，
            # 这里放行并记录，避免一次审核失败就让轮换空转。日志里能看出原因。
            verdict = (True, f"审核不可用，按拆解约束放行（{str(e)[:60]}）")

    with _lock:
        if len(_scope_cache) > 800:
            _scope_cache.clear()
        _scope_cache[key] = verdict
    return verdict


def _judge_many(seed: str, candidates: list[str]) -> dict[str, tuple[bool, str]]:
    """一次调用审核多条候选，返回 {候选: (是否在范围内, 理由)}。

    补充子方向时可能一次拿到 6-20 条，逐条审核就是 6-20 次模型调用；
    合成一次既快又不容易被限流。字面命中的仍走快路径，不进 prompt。
    """
    out: dict[str, tuple[bool, str]] = {}
    pending: list[str] = []
    for text in candidates:
        if _lexical_in_scope(seed, text):
            out[text] = (True, "包含母方向关键词")
        else:
            pending.append(text)
    if pending:
        listing = "\n".join(f"{i}. {t}" for i, t in enumerate(pending, 1))
        try:
            data = llm.chat_json(
                JUDGE_MANY_SYSTEM,
                f"## 母方向\n{seed}\n\n## 候选子方向\n{listing}",
                temperature=0.0,
            )
            verdicts = data.get("verdicts") if isinstance(data, dict) else None
            if not isinstance(verdicts, list):
                raise ValueError("批量审核输出缺少 verdicts 数组")
            for v in verdicts:
                if not isinstance(v, dict):
                    continue
                try:
                    idx = int(v.get("index"))
                except (TypeError, ValueError):
                    continue
                if not 1 <= idx <= len(pending) or not isinstance(v.get("in_scope"), bool):
                    continue
                out[pending[idx - 1]] = (bool(v["in_scope"]), str(v.get("reason") or "")[:120])
        except Exception as e:  # noqa: BLE001
            _emit("warn", f"批量审核失败，逐条复核：{str(e)[:80]}")
        # 模型漏答或整体失败的条目，用逐条审核补齐（带缓存）
        for text in pending:
            if text not in out:
                out[text] = in_scope(seed, text)

    with _lock:
        if len(_scope_cache) + len(out) > 800:
            _scope_cache.clear()
        for text, verdict in out.items():
            _scope_cache[(_norm(seed), _norm(text))] = verdict
    return out


# --------------------------------------------------------------------------
# 计划维护
# --------------------------------------------------------------------------
def _add_plan(items: list[str], source: str) -> list[dict]:
    entries: list[dict] = []
    with _lock:
        rid = _state["run_id"]
        seq = len(_state["plan"])
        for text in items:
            seq += 1
            entries.append(
                {
                    "id": uuid.uuid4().hex[:10],
                    "direction": text,
                    "seq": seq,
                    "status": "pending",
                    "rounds": 0,
                    "points": 0,
                    "first_round": 0,
                    "last_round": 0,
                    "note": source,
                    "sessions": [],
                    "session_total": 0,
                }
            )
        _state["plan"].extend(entries)
    if not rid:
        # 正常流程不会走到这里（start 里先建任务再加方向）；真出现了说明调用
        # 顺序错了，给一条明确告警，比悄悄写一堆查不回来的孤儿行好。
        _emit("warn", "子方向未关联任务，跳过落库（内存态仍可用）")
        return entries
    try:
        memory.curriculum_add_directions(rid, entries)
    except Exception as e:  # noqa: BLE001
        _emit("warn", f"子方向落库失败：{str(e)[:80]}")
    return entries


def _reject(entry: dict, reason: str) -> None:
    with _lock:
        entry["status"] = "rejected"
        entry["note"] = reason[:120]
    try:
        memory.curriculum_update_direction(entry["id"], status="rejected", note=reason[:120])
    except Exception:
        pass


def _replenish(exclude: set[str]) -> bool:
    """子方向池用尽时补充新的合法子方向。"""
    if not config.get("rotation_replenish", True):
        return False
    with _lock:
        seed = _state["seed"]
        count = int(_state["subdirection_count"])
        known = [e["direction"] for e in _state["plan"]]
        # 越界被丢弃的方向只留作审计，不占池子额度：否则模型多跑偏几次，
        # 池子就被「已丢弃」的条目填满，补充机制反而再也补不进新方向。
        live = [e for e in _state["plan"] if e["status"] != "rejected"]
        cap = max(count * PLAN_CAP_FACTOR, count)
        room = cap - len(live)
    if room <= 0:
        return False

    items, source = _split(seed, min(count, room), exclude=known + sorted(exclude))
    if config.get("rotation_boundary_check", True) and items:
        verdicts = _judge_many(seed, items)
        kept = []
        for text in items:
            ok, why = verdicts.get(text, (True, ""))
            if ok:
                kept.append(text)
            else:
                _emit("warn", f"丢弃越界子方向：{text[:60]}（{why or '不在母方向范围内'}）")
        items = kept
    if not items:
        return False
    _add_plan(items, f"replenish:{source}")
    _emit("info", f"子方向池已补充 {len(items)} 个新方向")
    return True


def _has_pending(exclude: set[str]) -> bool:
    with _lock:
        return any(
            e["status"] == "pending" and _norm(e["direction"]) not in exclude
            for e in _state["plan"]
        )


def _next_candidate(exclude: set[str], allow_replenish: bool = True) -> dict | None:
    with _lock:
        plan = list(_state["plan"])
    fresh = [e for e in plan if e["status"] == "pending" and _norm(e["direction"]) not in exclude]
    if fresh:
        return random.SystemRandom().choice(fresh)
    if allow_replenish and _replenish(exclude):
        with _lock:
            fresh = [
                e for e in _state["plan"]
                if e["status"] == "pending" and _norm(e["direction"]) not in exclude
            ]
        if fresh:
            return random.SystemRandom().choice(fresh)
    # 都学过了：把已完成/中断过的重新放回待学（当前方向和最近窗口内的除外）
    recycle = [
        e for e in plan
        if e["status"] in ("done", "stopped") and _norm(e["direction"]) not in exclude
    ]
    if recycle:
        entry = random.SystemRandom().choice(recycle)
        with _lock:
            entry["status"] = "pending"
        try:
            memory.curriculum_update_direction(entry["id"], status="pending")
        except Exception:
            pass
        return entry
    return None


def _fallback_candidate(seed: str, exclude: set[str]) -> dict | None:
    """最后一道保险：用本地池按母方向拼一个，一定在范围内。"""
    picked = directions.compose(seed, directions.deck(seed), sorted(exclude), 1)
    if not picked:
        return None
    return _add_plan(picked, "fallback")[0]


def _choose(seed: str) -> dict | None:
    """按「避开最近 + 边界校验」挑下一个子方向；不合格的丢弃并重选。"""
    window = max(1, int(config.get("rotation_recent_window", 3)))
    with _lock:
        recent = [_state["current"]] if _state["current"] else []
        recent += [s.get("to", "") for s in _state["switches"][-window:]]
    exclude = {_norm(s) for s in recent if s}
    tried: set[str] = set()

    # 补充子方向要问一次模型，只做一次：候选被连续拒绝时如果每次都补，
    # 一次换向可能连打 8 次模型，既慢又容易被限流。
    if not _has_pending(exclude):
        _replenish(exclude)

    for _ in range(PICK_TRIES):
        entry = _next_candidate(exclude | tried, allow_replenish=False)
        if entry is None:
            break
        text = entry["direction"]
        if not config.get("rotation_boundary_check", True):
            return entry
        ok, why = in_scope(seed, text)
        if ok:
            return entry
        _reject(entry, why)
        tried.add(_norm(text))
        _emit("warn", f"丢弃越界子方向：{text[:60]}（{why or '不在母方向范围内'}）")

    entry = _fallback_candidate(seed, exclude | tried)
    if entry is not None:
        _emit("info", "候选都不合格，改用本地拼装的合法方向")
    return entry


def _switch(reason: str) -> dict | None:
    with _lock:
        seed = _state["seed"]
        prev = _state["current"]
        prev_id = _state["current_id"]
        total_done = _state["total_rounds_done"]
        quota = _state["rounds_per_direction"]

    entry = _choose(seed)
    if entry is None:
        _emit("warn", "找不到可用的合法子方向，轮换提前结束")
        _finish("no_direction")
        return None

    prev_row = None
    with _lock:
        if prev_id:
            old = next((e for e in _state["plan"] if e["id"] == prev_id), None)
            if old is not None and old["status"] == "active":
                old["status"] = "done" if old["rounds"] >= quota else "stopped"
                prev_row = dict(old)
        entry["status"] = "active"
        entry["first_round"] = entry["first_round"] or total_done + 1
        _state.update(
            current=entry["direction"],
            current_id=entry["id"],
            rounds_in_current=0,
            stage="switching",
        )
        record = {
            "ts": time.time(),
            "from": prev,
            "to": entry["direction"],
            "reason": reason,
            "at_round": total_done,
        }
        _state["switches"].append(record)

    try:
        # 关闭旧方向、激活新方向、记切换历史：三处必须一起成功
        memory.curriculum_save_switch(
            _state["run_id"], prev_row,
            {"id": entry["id"], "first_round": entry["first_round"]},
            {"from": prev, "to": entry["direction"], "reason": reason, "at_round": total_done},
        )
    except Exception:
        pass
    config.update({"last_direction": entry["direction"]})
    # 总上限可能不够一个完整配额：提示里写实际会学的轮数，否则用户会以为
    # 这个方向还要跑满 100 轮，结果下一轮就结束了。
    planned = max(0, min(_state["rounds_per_direction"], _state["total_round_limit"] - total_done))
    tail = f"，该方向计划再学 {planned} 轮" + ("（受总轮次上限截断）" if planned < _state["rounds_per_direction"] else "")
    _emit(
        "stage", f"轮换 · 切换方向（{reason}）",
        direction=entry["direction"], total_round=total_done,
    )
    _emit(
        "ok",
        f"切换方向：{'「' + prev[:40] + '」→ ' if prev else ''}「{entry['direction'][:60]}」{tail}",
        direction=entry["direction"],
    )
    return entry


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def _run_batch(direction: str, rounds: int, run_id: str) -> tuple[int, int, str]:
    """把一批轮次交给学习引擎并等它跑完，返回 (完成轮数, 新增知识点, 错误)。"""
    try:
        res = evolve.engine.start(direction, rounds, _internal=True)
    except Exception as e:  # noqa: BLE001
        # 线程都起不来（句柄/内存耗尽）时，Engine.start 会把异常抛出来。
        # 这属于「这批没跑起来」，不是「轮换逻辑坏了」，交给失败计数处理，
        # 不能让整个任务直接以 error 结束。
        return 0, 0, f"学习引擎启动异常：{str(e)[:120]}"
    if not res.get("ok"):
        return 0, 0, str(res.get("msg") or "学习引擎拒绝启动")
    st = evolve.engine.state
    while not evolve.engine.wait_idle(0.5):
        # 暂停 / 结束 / 跳过 都通过引擎自带的停止口收尾：正在读的页面不会白读，
        # 也不会留下半条入库记录。跳过一次 100 轮的批次不能等到跑完才生效。
        if _stop_evt.is_set() or _state["paused"] or _state["skip_requested"]:
            evolve.engine.stop()
        # 任务已经被结束或被新任务接管：这批结果不能再算到任何人头上，
        # 也不该继续占着学习引擎。
        if _state["run_id"] != run_id:
            evolve.engine.stop()
            return 0, 0, "任务已结束"
    return int(st.get("completed") or 0), int(st.get("points") or 0), ""


def _begin_batch() -> tuple[str, int, str, str] | None:
    """决定这一批学哪个方向、跑几轮。返回 None 表示本轮无事可做。"""
    finish = False
    switch_reason = ""
    with _lock:
        if not _state["active"]:
            return None
        if _stop_evt.is_set():
            # 停止信号可能在「上批结算完」和「这一批开始」之间到达；
            # 这里再挡一次，避免刚点结束又起了一批。
            return None
        if _state["paused"]:
            # 批次跑完就把「暂停中」落成「已暂停」，否则界面会一直显示正在暂停
            if not _state["in_batch"]:
                _state["stage"] = "paused"
            return None
        if _state["total_rounds_done"] >= _state["total_round_limit"]:
            finish = True
        elif _state["skip_requested"] and _state["current"]:
            # 手动跳过：不看配额，直接换下一个方向
            _state["skip_requested"] = False
            switch_reason = "手动跳过"
        elif (
            not _state["current"]
            or _state["rounds_in_current"] >= _state["rounds_per_direction"]
        ):
            switch_reason = (
                f"完成 {_state['rounds_per_direction']} 轮"
                if _state["current"] else "开始首个子方向"
            )

    # 结算要查库、换向可能调模型做边界审核，两者都可能耗到秒级。
    # 放在锁外执行，状态轮询（界面每 6 秒一次）才不会被卡住。
    if finish:
        _finish("total_rounds")
        return None
    if switch_reason and _switch(switch_reason) is None:
        return None

    with _lock:
        # 换向/结算期间用户可能已经暂停或结束，这里按最新状态重新判断
        if not _state["active"] or _stop_evt.is_set() or _state["paused"]:
            return None
        batch = min(
            BATCH_MAX,
            _state["rounds_per_direction"] - _state["rounds_in_current"],
            _state["total_round_limit"] - _state["total_rounds_done"],
        )
        if batch <= 0:
            _state.update(in_batch=False, running=False)
            return None
        _state.update(in_batch=True, running=True, stage="learning")
        return _state["current"], batch, _state["current_id"], _state["run_id"]


def _account_batch(
    entry_id: str, run_id: str, direction: str, done: int, points: int, err: str
) -> None:
    """把一批结果记到内存与数据库，再决定继续、等待还是暂停。"""
    # 引擎被别的重任务占着属于「排队」，不是「跑空」，不能计入失败
    busy = bool(err and "已有学习任务" in err)
    # 用户主动暂停 / 结束 / 跳过时，这一批本来就会提前收尾，
    # 一轮没跑完是预期结果，同样不能记成「跑空」——否则连点几次跳过就暂停了。
    interrupted = _stop_evt.is_set() or _state["paused"] or _state["skip_requested"]

    with _lock:
        # 这一批可能横跨了「用户结束任务」或「新任务启动」：两者都不能把结果
        # 记到当前状态上，否则新任务的轮次和进度会凭空多出来。
        if _state["run_id"] != run_id or not _state["active"]:
            return
        _state.update(in_batch=False, running=False)
        entry = next((e for e in _state["plan"] if e["id"] == entry_id), None)
        if done > 0:
            _state["failures"] = 0
            _state["blocked"] = ""
            _state["retry_in"] = 0
            _state["total_rounds_done"] += done
            _state["rounds_in_current"] += done
            if entry is not None:
                entry["rounds"] += done
                entry["points"] += points
                entry["last_round"] = _state["total_rounds_done"]
                session = str(evolve.engine.state.get("session_id") or "")
                if session:
                    # 记下这批是哪个学习会话，汇总里能从方向追到具体学习记录
                    entry.setdefault("sessions", []).append(
                        {"id": session, "rounds": done, "points": points, "at": time.time()}
                    )
                    entry["sessions"] = entry["sessions"][-20:]
                    # 明细只留最近 20 条，总数单独累加：截断后仍能说清「学了几次」
                    entry["session_total"] = int(entry.get("session_total") or 0) + 1
        elif not busy and not interrupted:
            _state["failures"] += 1
        snap = {
            "rounds": entry["rounds"] if entry else 0,
            "points": entry["points"] if entry else 0,
            "sessions": list(entry.get("sessions") or []) if entry else [],
            "session_total": int(entry.get("session_total") or 0) if entry else 0,
            "total": _state["total_rounds_done"],
            "in_current": _state["rounds_in_current"],
            "failures": _state["failures"],
            "quota": _state["rounds_per_direction"],
        }

    if entry_id:
        try:
            memory.curriculum_save_batch(
                entry_id, run_id,
                rounds=snap["rounds"], points=snap["points"], last_round=snap["total"],
                sessions=json.dumps(snap["sessions"], ensure_ascii=False),
                session_total=snap["session_total"],
                total_rounds=snap["total"],
            )
        except Exception:
            pass

    if done == 0:
        if interrupted:
            return
        if busy:
            # 等它结束继续；原因只在变化时提示一次，避免每 3 秒刷一条
            with _lock:
                first = _state["blocked"] != err
                _state["blocked"] = err
            if first:
                _emit("info", "学习引擎正被其他任务占用，等它结束后自动继续")
            time.sleep(3)
            return
        # 一次跑空可能是 Ollama 刚重启、限流；阈值可配置（网络差可以调大，
        # 排查时可以调成 1 立刻暴露问题）。
        threshold = int(config.get("rotation_max_failures", 2))
        if snap["failures"] < threshold:
            _emit("warn", f"本批没有推进任何轮次（{err or '无新增'}），稍后重试")
            time.sleep(2)
            return
        if snap["failures"] >= MODEL_RETRY_LIMIT:
            _emit(
                "error",
                f"连续 {snap['failures']} 次没能推进学习，轮换暂停：{err or '模型或网络不可用'}",
            )
            pause()
            return
        # 退避重试而不是直接暂停：Ollama 重启、开机自启未就绪都只是暂时的。
        # 用事件等待而不是 sleep，用户点暂停/结束能立刻打断。
        delay = min(
            MODEL_BACKOFF_MAX,
            MODEL_BACKOFF_BASE * 2 ** (snap["failures"] - threshold),
        )
        with _lock:
            _state["stage"] = "waiting_model"
            _state["retry_in"] = delay
        _emit("warn", f"模型暂时不可用，{delay} 秒后自动重试（第 {snap['failures']} 次未推进）")
        _wake_evt.wait(delay)
        _wake_evt.clear()
        return

    if snap["in_current"] >= snap["quota"]:
        _emit(
            "ok",
            f"「{direction[:50]}」已完成 {snap['in_current']} 轮，准备换方向（累计 {snap['total']} 轮）",
            total_round=snap["total"],
        )


def _iteration() -> None:
    plan = _begin_batch()
    if plan is None:
        return
    direction, batch, entry_id, run_id = plan
    done, points, err = _run_batch(direction, batch, run_id)
    _account_batch(entry_id, run_id, direction, done, points, err)


def _bootstrap() -> tuple[list[str], str]:
    """确定本次任务的初始子方向。

    用户点过「预览子方向」并直接开始的话，沿用预览结果——否则模型每次调用
    都可能给出不同的一组，用户看到的和实际学的不一致。预览结果同样要过
    清洗与边界审核，不合格的丢弃；全丢光就退回重新拆解。
    """
    with _lock:
        seed = _state["seed"]
        count = int(_state["subdirection_count"])
        preset = list(_state["preset"])
        _state["preset"] = []

    if preset:
        items = _clean_items(preset, seed, set())[:count]
        if items and config.get("rotation_boundary_check", True):
            verdicts = _judge_many(seed, items)
            kept = []
            for text in items:
                ok, why = verdicts.get(text, (True, ""))
                if ok:
                    kept.append(text)
                else:
                    _emit("warn", f"预览方向不在范围内，已丢弃：{text[:60]}（{why or '越界'}）")
            items = kept
        if items:
            _emit("info", f"沿用预览的 {len(items)} 个子方向")
            return items, "preview"

    return _split(seed, count)


def _loop() -> None:
    try:
        with _lock:
            quota = _state["rounds_per_direction"]
            limit = _state["total_round_limit"]
        _state["stage"] = "splitting"
        _emit("stage", "轮换 · 拆解子方向", stage="splitting")
        items, source = _bootstrap()
        if not items:
            _emit("error", "没能拆出可用的子方向，请换一个更明确的领域名后重试")
            _finish("no_plan")
            return
        _add_plan(items, source)
        _emit(
            "ok",
            f"已确定 {len(items)} 个子方向，每个连续学 {quota} 轮，总上限 {limit} 轮",
            items=items,
        )
        for text in items:
            _emit("info", f"子方向：{text}", direction=text)
    except Exception as e:  # noqa: BLE001
        _emit("error", f"方向轮换启动失败：{e}")
        memory.log_event("error", str(e), {"trace": traceback.format_exc()[:2000]})
        _finish("error")
        return

    while not _stop_evt.is_set():
        try:
            _iteration()
        except Exception as e:  # noqa: BLE001
            _emit("error", f"方向轮换异常：{e}")
            memory.log_event("error", str(e), {"trace": traceback.format_exc()[:2000]})
            _finish("error")
            return
        if _stop_evt.is_set() or not _state["active"]:
            break
        _wake_evt.wait(0.5)
        _wake_evt.clear()

    if _state["active"]:
        _finish("stopped")


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def _validate(
    seed: str,
    rounds_per_direction: int | None,
    total_rounds: int | None,
    subdirection_count: int | None,
) -> tuple[str, int, int, int]:
    """参数校验只此一处：start 与 preview 共用，避免两边规则漂移。

    返回 (规范化后的母方向, 每方向轮数, 总上限, 子方向数量)。
    """
    seed = " ".join(str(seed or "").split())
    if not seed:
        raise ValueError("请填写一个大致的学习方向")
    if len(seed) > 200:
        raise ValueError("学习方向不能超过 200 个字符")
    quota = int(rounds_per_direction if rounds_per_direction is not None
                else config.get("rotation_rounds_per_direction", 100))
    limit = int(total_rounds if total_rounds is not None
                else config.get("rotation_total_rounds", 300))
    count = int(subdirection_count if subdirection_count is not None
                else config.get("rotation_subdirection_count", 6))
    if not 1 <= quota <= 2000:
        raise ValueError("每个子方向的轮数必须是 1 到 2000 的整数")
    if not 1 <= limit <= 100000:
        raise ValueError("总轮次上限必须是 1 到 100000 的整数")
    if not 2 <= count <= 20:
        raise ValueError("子方向数量必须是 2 到 20 的整数")
    return seed, quota, limit, count


@tasks.exclusive("方向轮换")
def start(
    seed: str,
    rounds_per_direction: int | None = None,
    total_rounds: int | None = None,
    subdirection_count: int | None = None,
    items: list[str] | None = None,
) -> dict:
    try:
        seed, quota, limit, count = _validate(
            seed, rounds_per_direction, total_rounds, subdirection_count
        )
    except (TypeError, ValueError) as e:
        return {"ok": False, "msg": str(e)}
    # 预览过的方向由调用方带回来；这里只做粗筛，真正的清洗与边界审核在
    # 后台线程的 _bootstrap 里做（那里失败还能退回重新拆解）。
    preset = [s for s in (items or []) if isinstance(s, str) and s.strip()][:20]
    with _lock:
        if _state["active"]:
            return {"ok": False, "msg": "已有方向轮换任务在跑，请先暂停或结束"}
        old = _state.get("thread")
        # stop() 在「批次正在跑」时不立刻结算，线程要等这一轮收尾才退出；
        # 这个窗口里启动新任务会覆盖状态，让旧线程把结果记到新任务账上。
        if old is not None and old.is_alive():
            return {"ok": False, "msg": "上一次轮换任务正在收尾，请稍候再试"}

    # 参数写回配置，下次打开页面就是上次的设置
    try:
        config.update({
            "rotation_rounds_per_direction": quota,
            "rotation_total_rounds": limit,
            "rotation_subdirection_count": count,
            "last_direction": seed,
        })
    except ValueError as e:
        return {"ok": False, "msg": str(e)}

    rid = memory.curriculum_start_run(seed, quota, limit, count)
    try:
        memory.curriculum_prune(KEEP_RUNS)
    except Exception:
        pass  # 清理失败不该挡住新任务
    _stop_evt.clear()
    _wake_evt.clear()
    with _lock:
        _scope_cache.clear()
        _state.update(
            active=True, running=False, paused=False, in_batch=False,
            stage="starting", seed=seed, run_id=rid, plan=[], current="",
            current_id="", rounds_in_current=0, rounds_per_direction=quota,
            total_round_limit=limit, total_rounds_done=0, subdirection_count=count,
            switches=[], summary=None, error="", stop_reason="",
            started_at=time.time(), finished_at=0.0, failures=0, blocked="",
            skip_requested=False, retry_in=0, preset=preset,
        )
        thread = threading.Thread(target=_loop, name="curriculum", daemon=True)
        _state["thread"] = thread
    try:
        thread.start()
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state.update(active=False, running=False, stage="idle", error=str(e))
        memory.curriculum_update_run(rid, status="error", ended_at=time.time(), stop_reason=str(e))
        return {"ok": False, "msg": f"轮换线程启动失败：{e}"}

    _emit(
        "ok",
        f"方向轮换已启动：母方向「{seed}」，每方向 {quota} 轮，总上限 {limit} 轮",
        seed=seed, run_id=rid,
    )
    if limit < quota:
        # 不拦：用户可能就想先试一点。但必须说清后果，否则会以为首个子方向
        # 也要学满 quota 轮，结果几轮后就结束了。
        _emit(
            "warn",
            f"总轮次上限 {limit} 小于每方向轮数 {quota}：首个子方向只会学 {limit} 轮就结束",
        )
    return {"ok": True, "run_id": rid, "state": state()}


def pause() -> dict:
    with _lock:
        if not _state["active"]:
            return {"ok": False, "msg": "没有正在运行的轮换任务"}
        if _state["paused"]:
            return {"ok": True, "msg": "已经是暂停状态"}
        in_batch = _state["in_batch"]
        _state.update(paused=True, stage="pausing" if in_batch else "paused")
        current = _state["current"]
        left = _state["rounds_per_direction"] - _state["rounds_in_current"]
    _wake_evt.set()
    evolve.engine.stop()
    _emit(
        "warn",
        "已暂停：当前这一轮跑完就停住" + (f"，恢复后继续「{current[:40]}」剩余 {left} 轮" if current else ""),
        stage="paused",
    )
    return {"ok": True, "state": state()}


def skip() -> dict:
    """跳过当前子方向：剩余轮次不再学，换下一个（仍走边界校验与最近窗口）。

    和「暂停」不同，这是对内容的选择而不是对进度的控制：用户看到某个方向
    不是自己想学的，应该能立刻换掉，而不是等满 100 轮。
    """
    with _lock:
        if not _state["active"]:
            return {"ok": False, "msg": "没有正在运行的轮换任务"}
        if not _state["current"]:
            return {"ok": False, "msg": "还没有选中方向，稍等拆解完成"}
        if _state["skip_requested"]:
            return {"ok": True, "msg": "已经安排了跳过"}
        _state["skip_requested"] = True
        current = _state["current"]
        in_batch = _state["in_batch"]
    _wake_evt.set()
    if in_batch:
        # 当前这一轮跑完就收尾，剩下的轮次不再学
        evolve.engine.stop()
    _emit(
        "warn",
        f"已跳过「{current[:40]}」：{'当前这一轮跑完' if in_batch else '马上'}换下一个方向",
        stage="switching",
    )
    return {"ok": True, "state": state()}


def resume() -> dict:
    with _lock:
        if not _state["active"]:
            return {"ok": False, "msg": "没有可恢复的轮换任务"}
        if not _state["paused"]:
            return {"ok": True, "msg": "任务正在运行"}
        _state.update(paused=False, stage="learning")
        # 恢复等于「重新开始跑」：上一次的等待原因和失败计数都过期了。
        # 不清掉的话，恢复后第一次失败就会触发「连续 2 次」的暂停判定。
        _state["blocked"] = ""
        _state["failures"] = 0
        current = _state["current"]
        left = _state["rounds_per_direction"] - _state["rounds_in_current"]
    _wake_evt.set()
    _emit("ok", f"已恢复：继续「{current[:50]}」剩余 {left} 轮", stage="learning")
    return {"ok": True, "state": state()}


def stop() -> dict:
    _stop_evt.set()
    _wake_evt.set()
    with _lock:
        if not _state["active"]:
            return {"ok": True, "msg": "没有正在运行的轮换任务", "summary": summary()}
        _state["stage"] = "stopping"
        in_batch = _state["in_batch"]
    evolve.engine.stop()
    # 没有批次在跑（暂停中 / 刚换完方向）时线程不会自己走到收尾点，
    # 这里直接结算，保证「结束」按钮立刻能看到汇总。
    if not in_batch:
        _finish("stopped")
    return {"ok": True, "summary": summary()}


def _counts_as_learned(row: dict) -> bool:
    """该方向是否算「学出了成果」。

    越界丢弃的不算（被审核拦下，没有轮次也没有知识）；待学但已经有轮次的算
    ——状态还没来得及切换时（批次刚结算完），它确实学出了东西，不该从汇总里消失。
    """
    if row.get("status") == "rejected":
        return False
    return int(row.get("rounds") or 0) > 0


def _build_summary() -> dict:
    with _lock:
        rid = _state["run_id"]
        seed = _state["seed"]
        total_done = _state["total_rounds_done"]
        limit = _state["total_round_limit"]
        quota = _state["rounds_per_direction"]
        started = _state["started_at"]
        finished = _state["finished_at"]
        reason = _state["stop_reason"]
        plan = [dict(e) for e in _state["plan"]]
        switches = [dict(s) for s in _state["switches"]]

    rows = []
    if rid:
        try:
            rows = memory.curriculum_list_directions(rid)
        except Exception:
            rows = []
    if not rows:
        rows = plan

    items = []
    wanted = [row for row in rows if _counts_as_learned(row)]
    skipped = [row["direction"] for row in rows if row.get("status") == "rejected"]
    # 代表结论一次查全：方向可能有几十个，逐个 topic 查就是几十次 SQL
    claims: dict[str, list[str]] = {}
    try:
        claims = memory.recent_claims([row["direction"] for row in wanted], 3)
    except Exception:
        claims = {}
    for row in wanted:
        sessions = row.get("sessions") or "[]"
        if isinstance(sessions, str):
            try:
                sessions = json.loads(sessions)
            except ValueError:
                sessions = []
        items.append(
            {
                "direction": row["direction"],
                "status": row.get("status", ""),
                "rounds": int(row.get("rounds") or 0),
                "points": int(row.get("points") or 0),
                "first_round": int(row.get("first_round") or 0),
                "last_round": int(row.get("last_round") or 0),
                "sessions": sessions if isinstance(sessions, list) else [],
                "session_total": int(row.get("session_total") or 0)
                or (len(sessions) if isinstance(sessions, list) else 0),
                "highlights": claims.get(row["direction"], []),
            }
        )

    # 按产出排序：汇总的第一屏应该是学得最多的方向，而不是计划创建顺序
    items.sort(key=lambda i: (-i["rounds"], i["first_round"]))
    return {
        "run_id": rid,
        "seed": seed,
        "total_rounds": total_done,
        "total_limit": limit,
        "rounds_per_direction": quota,
        "points_total": sum(i["points"] for i in items),
        "directions": items,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "switches": switches,
        # 首次选向也记在切换表里，但用户理解的「切换」是真正换到另一个方向
        "switch_count": sum(1 for s in switches if s.get("from")),
        "started_at": started,
        "finished_at": finished,
        "elapsed_seconds": max(0, int((finished or time.time()) - started)) if started else 0,
        "seconds_per_round": (
            round(max(0, (finished or time.time()) - started) / total_done, 1)
            if started and total_done else 0
        ),
        "stop_reason": reason,
    }


def _summary_line(summ: dict) -> str:
    names = [i["direction"] for i in summ["directions"] if i["rounds"]]
    skipped = summ.get("skipped_count") or 0
    tail = f"，丢弃 {skipped} 个越界方向" if skipped else ""
    return (
        f"方向轮换结束：共 {summ['total_rounds']} 轮 / 上限 {summ['total_limit']} 轮，"
        f"覆盖 {len(names)} 个方向，新增 {summ['points_total']} 条知识{tail}"
    )


def _summary_text(summ: dict) -> str:
    elapsed = int(summ.get("elapsed_seconds") or 0)
    cost = f"，耗时 {elapsed // 60} 分 {elapsed % 60} 秒" if elapsed else ""
    skipped = summ.get("skipped_count") or 0
    drop = f" · 丢弃越界方向 {skipped} 个" if skipped else ""
    lines = [
        f"方向轮换汇总 · 母方向「{summ['seed']}」",
        f"总轮次 {summ['total_rounds']} / 上限 {summ['total_limit']} · "
        f"每方向 {summ['rounds_per_direction']} 轮 · 切换 {summ['switch_count']} 次{cost}{drop}",
    ]
    for item in summ["directions"]:
        short = "（未学满）" if item["rounds"] < summ["rounds_per_direction"] else ""
        lines.append(
            f"- {item['direction']}：{item['rounds']} 轮 / 新增 {item['points']} 条{short}"
        )
        for h in item["highlights"][:2]:
            lines.append(f"    · {h[:90]}")
    lines.append(f"合计新增 {summ['points_total']} 条知识")
    return "\n".join(lines)


def _finish(reason: str) -> dict:
    with _lock:
        if not _state["active"]:
            return _state["summary"] or {}
        _state.update(
            active=False,
            running=False,
            paused=False,
            in_batch=False,
            stage="done" if reason == "total_rounds" else ("error" if reason == "error" else "stopped"),
            stop_reason=reason,
            finished_at=time.time(),
        )
        _stop_evt.set()
        _wake_evt.set()
        # 收尾时把当前方向结算掉，否则汇总里会一直挂着一条「进行中」
        current_id = _state["current_id"]
        entry = next((e for e in _state["plan"] if e["id"] == current_id), None)
        if entry is not None and entry["status"] == "active":
            entry["status"] = "done" if entry["rounds"] >= _state["rounds_per_direction"] else "stopped"
            try:
                memory.curriculum_update_direction(
                    entry["id"], status=entry["status"], rounds=entry["rounds"],
                    points=entry["points"], last_round=entry["last_round"],
                )
            except Exception:
                pass
        summ = _build_summary()
        text = _summary_text(summ)
        _state["summary"] = summ
        rid = _state["run_id"]

    status = {"total_rounds": "done", "error": "error", "no_plan": "error",
              "no_direction": "stopped"}.get(reason, "stopped")
    if rid:
        try:
            memory.curriculum_update_run(
                rid, status=status, total_rounds=summ["total_rounds"],
                ended_at=time.time(), stop_reason=reason, summary=text,
            )
        except Exception:
            pass
    _emit(
        "ok" if status == "done" else "warn",
        _summary_line(summ),
        summary=summ, stage="done", total_round=summ["total_rounds"],
    )
    return summ


def is_active() -> bool:
    """只回答「有没有轮换任务在跑」。

    任务准入检查（调度器每秒一次 + 每次独占任务启动）只需要这一个布尔值，
    没必要为它拷贝整份子方向计划。
    """
    with _lock:
        return bool(_state["active"])


def state(include_last: bool = False) -> dict:
    with _lock:
        plan = [dict(e) for e in _state["plan"]]
        switches = [dict(s) for s in _state["switches"][-20:]]
        idx = next((i for i, e in enumerate(plan) if e["id"] == _state["current_id"]), -1)
        total_done = _state["total_rounds_done"]
        limit = _state["total_round_limit"] or 0
        started = _state["started_at"]
        active = _state["active"]
        paused = _state["paused"]
        stop_reason = _state["stop_reason"]
        snap = {
            "active": active,
            "running": _state["running"],
            "paused": paused,
            "stage": _state["stage"],
            "status_text": (
                ("已暂停" if paused else ACTIVE_STAGE_TEXT.get(_state["stage"], "进行中"))
                if active else STATUS_TEXT.get(stop_reason, "未启动")
            ),
            "seed": _state["seed"],
            "run_id": _state["run_id"],
            "current": _state["current"],
            "rounds_in_current": _state["rounds_in_current"],
            "rounds_per_direction": _state["rounds_per_direction"],
            "total_rounds_done": total_done,
            "total_round_limit": _state["total_round_limit"],
            "subdirection_count": _state["subdirection_count"],
            "direction_index": idx + 1 if idx >= 0 else 0,
            "direction_total": len(plan),
            "progress": round(total_done / limit, 4) if limit else 0.0,
            "plan": plan,
            "switches": switches,
            "switch_count": sum(1 for s in _state["switches"] if s.get("from")),
            "started_at": started,
            "finished_at": _state["finished_at"],
            "stop_reason": stop_reason,
            "error": _state["error"],
            "blocked": _state["blocked"],
            "skip_requested": _state["skip_requested"],
            "retry_in": _state["retry_in"],
            "failures": _state["failures"],
            "elapsed": round(time.time() - started) if started and active else 0,
        }
        summ = _state["summary"]
    # 进程重启后内存是空的，从库里捞上一次任务，进度和汇总不至于凭空消失。
    # 只在界面主动查询时做（include_last），任务准入检查每秒都调 state()，
    # 不能让它顺带查库。
    if include_last and not snap["active"] and not snap["plan"] and not summ:
        snap["last_run"] = _last_run_view()
    if include_last:
        # 面板上的「最近任务」：只查 5 条，和当前进度一起返回，省一次请求
        try:
            snap["history"] = [
                {
                    "run_id": r["id"],
                    "seed": r["seed"],
                    "status": r["status"],
                    "status_text": (
                        "进行中" if r["status"] == "running"
                        else STATUS_TEXT.get(r.get("stop_reason") or "", "已结束")
                    ),
                    "total_rounds": r["total_rounds"],
                    "total_limit": r["total_limit"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                }
                for r in memory.curriculum_runs(5)
            ]
        except Exception:
            snap["history"] = []
    return snap


def _last_run_view() -> dict | None:
    try:
        run = memory.curriculum_last_run()
        if not run:
            return None
        rows = memory.curriculum_list_directions(run["id"])
        started, ended = run["started_at"], run["ended_at"]
        return {
            "run_id": run["id"],
            "seed": run["seed"],
            "status": run["status"],
            "total_rounds": run["total_rounds"],
            "total_limit": run["total_limit"],
            "rounds_per_direction": run["rounds_per_direction"],
            "started_at": started,
            "ended_at": ended,
            "elapsed_seconds": max(0, int((ended or started) - started)) if started else 0,
            "directions": [
                {"direction": r["direction"], "rounds": r["rounds"], "points": r["points"],
                 "status": r["status"]}
                for r in rows if _counts_as_learned(r)
            ],
            "skipped_count": sum(1 for r in rows if r["status"] == "rejected"),
        }
    except Exception:
        return None


def summary() -> dict:
    with _lock:
        if _state["summary"] and not _state["active"]:
            return _state["summary"]
    if _state["run_id"]:
        return _build_summary()
    last = _last_run_view()
    return {"last_run": last} if last else {}


def summary_text() -> str:
    """汇总的纯文本版本，供导出/复制或脚本直接取用。

    没有可汇总的方向时返回空串，调用方据此判断「这次没学出东西」，
    不用去猜 JSON 里的字段组合。
    """
    summ = summary()
    if not summ or not summ.get("directions"):
        return ""
    return _summary_text(summ)


def preview(seed: str, count: int | None = None) -> dict:
    """只拆解、不启动：让用户在开始前看清会被拆成哪些子方向。"""
    seed, _, _, count = _validate(seed, None, None, count)
    items, source = _split(seed, count)
    return {"seed": seed, "items": items, "source": source}


def history(limit: int = 20) -> list[dict]:
    try:
        return memory.curriculum_runs(limit)
    except Exception:
        return []


def recover_stale() -> None:
    """把「上一次进程留下的 running 任务」标为中断。

    只处理本进程启动之前创建的记录：同一份数据库可能被两个进程打开
    （例如同时开着正式实例和测试实例），不能把别人正在跑的任务判死。
    """
    try:
        for run in memory.curriculum_runs(20):
            if run.get("status") != "running":
                continue
            if (run.get("started_at") or 0) >= BOOT_TS:
                continue
            memory.curriculum_update_run(
                run["id"], status="stopped", ended_at=time.time(), stop_reason="进程重启"
            )
    except Exception:
        pass
