"""Ollama 推理后端封装：对话、结构化输出、向量化、模型拉取。"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Iterable, Iterator

import httpx

from . import config, httpc


class LLMError(RuntimeError):
    pass


def _base() -> str:
    return config.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")


def _client(timeout: float | None = None) -> "httpc.Borrowed":
    """共享客户端（连接池复用）。注意：退出 `with` 不会关闭它——连接要留给后续调用。

    超时按请求覆盖（httpx per-request timeout），与原先「每次新建时设一次性超时」
    等效。ollama_url / 代理变更时 config.update() 会调 httpc.reset() 重建。
    """
    return httpc.borrow_client(
        "llm", base_url=_base(), timeout=timeout or 300.0
    )


def _parse_tags(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for m in payload.get("models", []):
        out.append(
            {
                "name": m.get("name"),
                "size_gb": round((m.get("size") or 0) / (1024 ** 3), 2),
                "params": (m.get("details") or {}).get("parameter_size"),
                "quant": (m.get("details") or {}).get("quantization_level"),
                "caps": m.get("capabilities") or [],
            }
        )
    return out


#: /api/tags 的「先返回旧值、后台刷新」缓存。
#:
#: 为什么不能用普通 TTL：`/api/status` 每 6 秒轮询一次，而 Ollama 没启动时
#: 这个请求要 2 秒才失败（连接被拒 + 重试）。TTL 一到，轮询线程就同步等这 2 秒，
#: 整轮刷新（状态栏、图表、建议全部排队）跟着卡住。实测空转 10 分钟就卡 75 次。
#: 现在调用方永远拿「最近一次探测结果」，过期只触发**后台线程**去刷，
#: 失败则指数退避（8s→16s→32s→60s 封顶），不再每 8 秒重付一次失败代价。
_tags_cache: dict[str, Any] = {
    "probed": False,      # 是否至少完成过一次探测（成功或失败）
    "healthy": False,
    "models": [],
    "ts": 0.0,            # 上次探测完成的 monotonic 时刻
    "refreshing": False,  # 是否有后台探测在跑（防止并发重复请求）
    "fail_streak": 0,     # 连续失败次数（退避用）
    "next_try": 0.0,      # 退避期内不允许再探测
}
_tags_lock = threading.Lock()
TAGS_TTL = 8.0
TAGS_BACKOFF_MAX = 60.0
TAGS_TIMEOUT = 5.0


def _invalidate_tags() -> None:
    """让下次调用重新探测（拉完模型后用），但不清掉已有结果，避免界面闪空。"""
    with _tags_lock:
        _tags_cache["ts"] = 0.0
        _tags_cache["next_try"] = 0.0
        _tags_cache["fail_streak"] = 0


def _refresh_tags() -> None:
    """真正打一次 /api/tags。只由 tags() 在后台线程或 fresh 模式调用。"""
    now = time.monotonic()
    try:
        r = _client().get("/api/tags", timeout=TAGS_TIMEOUT)
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}")
        models = _parse_tags(r.json())
    except Exception:
        with _tags_lock:
            c = _tags_cache
            c["probed"] = True
            c["healthy"] = False
            c["ts"] = now
            c["fail_streak"] += 1
            # 2**(n-1) 指数退避：连不上时不要每 8 秒再撞一次 2 秒的墙
            c["next_try"] = now + min(
                TAGS_TTL * (2 ** (c["fail_streak"] - 1)), TAGS_BACKOFF_MAX
            )
            c["refreshing"] = False
        return
    with _tags_lock:
        c = _tags_cache
        c["probed"] = True
        c["healthy"] = True
        c["models"] = models
        c["ts"] = now
        c["fail_streak"] = 0
        c["next_try"] = 0.0
        c["refreshing"] = False


def tags(*, fresh: bool = False) -> tuple[bool, list[dict[str, Any]]]:
    """返回 (服务是否健康, 模型清单)，**不阻塞**调用方。

    默认走缓存：过期只在后台刷新，本次调用立刻返回上一次的结果。
    只有 fresh=True（刚拉完模型要立刻看到）才同步探测一次。
    首次调用返回 (False, []) 并以 tags_pending() 告知「还在探测」。
    """
    global _tags_cache
    if fresh:
        _refresh_tags()
        with _tags_lock:
            return _tags_cache["healthy"], [dict(m) for m in _tags_cache["models"]]

    now = time.monotonic()
    start = False
    with _tags_lock:
        c = _tags_cache
        stale = (now - c["ts"]) >= TAGS_TTL
        if not c["refreshing"] and (not c["probed"] or (stale and now >= c["next_try"])):
            c["refreshing"] = True
            start = True
        healthy, models = c["healthy"], [dict(m) for m in c["models"]]
    if start:
        threading.Thread(target=_refresh_tags, name="tags-probe", daemon=True).start()
    return healthy, models


def tags_pending() -> bool:
    """是否还没拿到首个结果 / 正在后台探测（前端据此显示「检测中」而不是「未连接」）。"""
    with _tags_lock:
        return (not _tags_cache["probed"]) or _tags_cache["refreshing"]


def healthy() -> bool:
    return tags()[0]


def list_models() -> list[dict[str, Any]]:
    """模型清单。与 healthy() 共用同一份 /api/tags 缓存，不再每次重新往返。"""
    return tags()[1]


def has_model(name: str) -> bool:
    short = name.split(":")[0]
    return any(m["name"] == name or m["name"].split(":")[0] == short for m in list_models())


def pull_model(name: str, on_progress=None) -> None:
    """拉取模型，on_progress(dict) 回调进度。"""
    with _client(3600).stream("POST", "/api/pull", json={"model": name, "stream": True}, timeout=3600) as r:
        if r.status_code != 200:
            raise LLMError(f"拉取 {name} 失败: HTTP {r.status_code}")
        for line in r.iter_lines():
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if evt.get("error"):
                raise LLMError(str(evt["error"]))
            if on_progress:
                on_progress(evt)
    # 模型清单变了，状态展示的缓存立刻失效（否则要等最多一个 TTL）
    _invalidate_tags()


def ensure_models(on_event=None) -> None:
    """保证主模型与向量模型存在，缺则自动拉取。"""
    main = config.get("model")
    emb = config.get("embed_model")
    for name, label in ((main, "推理模型"), (emb, "向量模型")):
        if has_model(name):
            continue
        if on_event:
            on_event(f"缺少{label} {name}，开始自动下载…")

        def _p(evt: dict) -> None:
            if on_event and evt.get("status"):
                pct = ""
                if evt.get("total"):
                    pct = f" {int(evt.get('completed', 0) * 100 / evt['total'])}%"
                on_event(f"{name}: {evt['status']}{pct}")

        pull_model(name, _p)
        if on_event:
            on_event(f"{label} {name} 就绪")


# --------------------------------------------------------------------------
# 对话
# --------------------------------------------------------------------------
def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    num_ctx: int | None = None,
    timeout: float | None = None,
    tools: list[dict] | None = None,
    think: bool | None = None,
) -> str:
    payload: dict[str, Any] = {
        "model": model or config.get("model"),
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": (
                temperature if temperature is not None else config.get("temperature", 0.3)
            ),
            "num_ctx": num_ctx or config.get("context_num", 16384),
        },
    }
    # 学习循环里每一步都要快速返回，默认关闭 thinking。
    # thinking 模型（qwen3 系）开启后会先输出数千 token 的推理过程，速度慢一个数量级。
    if think is not None:
        payload["think"] = think
    elif config.get("disable_thinking", True):
        payload["think"] = False
    if json_mode:
        payload["format"] = "json"
    if tools:
        payload["tools"] = tools
    try:
        r = _client(timeout).post("/api/chat", json=payload, timeout=timeout or 300.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        raise LLMError(f"调用模型失败：{e}") from e
    msg = data.get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content and msg.get("thinking"):
        content = msg["thinking"].strip()
    return content


def chat_stream(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float | None = None,
) -> Iterator[str]:
    payload = {
        "model": model or config.get("model"),
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": (
                temperature if temperature is not None else config.get("temperature", 0.3)
            ),
            "num_ctx": config.get("context_num", 16384),
        },
    }
    with _client(600).stream("POST", "/api/chat", json=payload, timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines():
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                piece = (evt.get("message") or {}).get("content")
                if piece:
                    yield piece


# --------------------------------------------------------------------------
# 结构化输出
# --------------------------------------------------------------------------
def extract_json(text: str) -> Any:
    """从模型输出里尽力抠出 JSON。"""
    text = text.strip()
    if not text:
        raise LLMError("模型返回为空")
    # 去掉 ```json 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 找最外层 { } 或 [ ]
    for opener, closer in (("{", "}"), ("[", "]")):
        s = text.find(opener)
        e = text.rfind(closer)
        if s != -1 and e > s:
            try:
                return json.loads(text[s : e + 1])
            except Exception:
                continue
    raise LLMError(f"无法解析 JSON：{text[:200]}")


def chat_json(
    system: str,
    user: str,
    *,
    model: str | None = None,
    retries: int = 2,
    **kw,
) -> Any:
    """要求模型输出 JSON，失败自动重试（重试时追加更严格的提示）。

    重试之间做指数退避 —— 网络抖动时立刻重试几乎必然再失败，
    等一下反而更容易成功；纯 JSON 格式问题则会因为追加了严格提示而改善。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        extra = "" if attempt == 0 else "\n\n【重要】上次输出不是合法 JSON。只输出 JSON 本体，不要任何解释、不要 Markdown 代码围栏。"
        try:
            raw = chat(
                [{"role": "system", "content": system + extra}, {"role": "user", "content": user}],
                model=model,
                json_mode=True,
                **kw,
            )
            return extract_json(raw)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise LLMError(f"结构化输出失败：{last}")


# --------------------------------------------------------------------------
# 向量化
# --------------------------------------------------------------------------
def embed(texts: Iterable[str], *, batch: int = 16) -> list[list[float]]:
    items = [t if t.strip() else " " for t in texts]
    out: list[list[float]] = []
    model = config.get("embed_model")
    c = _client(300)
    for i in range(0, len(items), batch):
        chunk = items[i : i + batch]
        try:
            r = c.post("/api/embed", json={"model": model, "input": chunk}, timeout=300)
            r.raise_for_status()
            out.extend(r.json().get("embeddings") or [])
        except httpx.HTTPError as e:
            raise LLMError(f"向量化失败（模型 {model} 是否已下载？）：{e}") from e
    return out
