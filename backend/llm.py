"""Ollama 推理后端封装：对话、结构化输出、向量化、模型拉取。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Iterator

import httpx

from . import config


class LLMError(RuntimeError):
    pass


def _base() -> str:
    return config.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")


def _client(timeout: float | None = None) -> httpx.Client:
    proxy = config.get("proxy") or None
    return httpx.Client(
        base_url=_base(),
        timeout=httpx.Timeout(timeout or 300.0, connect=10.0),
        proxy=proxy,
        trust_env=False,
    )


def healthy() -> bool:
    try:
        with _client(5) as c:
            return c.get("/api/tags").status_code == 200
    except Exception:
        return False


def list_models() -> list[dict[str, Any]]:
    try:
        with _client(10) as c:
            r = c.get("/api/tags")
            r.raise_for_status()
            out = []
            for m in r.json().get("models", []):
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
    except Exception:
        return []


def has_model(name: str) -> bool:
    short = name.split(":")[0]
    return any(m["name"] == name or m["name"].split(":")[0] == short for m in list_models())


def pull_model(name: str, on_progress=None) -> None:
    """拉取模型，on_progress(dict) 回调进度。"""
    with _client(3600) as c:
        with c.stream("POST", "/api/pull", json={"model": name, "stream": True}) as r:
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
        with _client(timeout) as c:
            r = c.post("/api/chat", json=payload)
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
    with _client(600) as c:
        with c.stream("POST", "/api/chat", json=payload) as r:
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
    with _client(300) as c:
        for i in range(0, len(items), batch):
            chunk = items[i : i + batch]
            try:
                r = c.post("/api/embed", json={"model": model, "input": chunk})
                r.raise_for_status()
                out.extend(r.json().get("embeddings") or [])
            except httpx.HTTPError as e:
                raise LLMError(f"向量化失败（模型 {model} 是否已下载？）：{e}") from e
    return out
