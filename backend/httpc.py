"""共享 HTTP 客户端：连接池复用。

背景：`llm` / `fetcher` / `search` 原先每次调用都 `httpx.Client(...)` 新建一个客户端，
用完后关闭。本地回环上实测新建一次约 7ms（连接池初始化 + TCP 握手 + 拆连接），
而自主进化一轮要打几十次（规划 1 次 + 每篇提炼 2 次 + 嵌入若干 + 每 URL 抓取 1 次
+ 每检索源 1 次），等于每轮白付几百毫秒，且随检索式/文档数线性增长。

`httpx.Client` 本身是线程安全的，因此按用途各建一个共享实例、全局复用即可：
连接池里的 keep-alive 连接直接复用，超时改按请求覆盖（httpx 支持 per-request
timeout），行为与原先「每次新建」完全一致，只是省掉反复建连。

配置变更（代理 / ollama_url / UA）后由 `config.update()` 调 `reset()` 重建，
避免共享客户端拿着过期设置。
"""
from __future__ import annotations

import threading
from typing import Any

import httpx

from . import config

_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}

#: A/B 基准开关。置 True 时 `get_client` 每次返回全新客户端（复刻优化前行为），
#: 关闭即恢复共享。仅供 scripts/bench 使用，正常流程恒为 False。
FORCE_NEW_CLIENT = False


def _build(
    *,
    base_url: str | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> httpx.Client:
    # base_url 为 None 时不能传该参数（httpx 会抛 TypeError）
    kw: dict[str, Any] = {}
    if base_url:
        kw["base_url"] = base_url
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers=headers,
        follow_redirects=follow_redirects,
        proxy=config.get("proxy") or None,
        trust_env=False,
        **kw,
    )


def get_client(
    key: str,
    *,
    base_url: str | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> httpx.Client:
    """按 key 取共享客户端；首次或已关闭时创建。线程安全。"""
    if FORCE_NEW_CLIENT:
        return _build(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            follow_redirects=follow_redirects,
        )
    with _lock:
        c = _clients.get(key)
        if c is None or c.is_closed:
            c = _build(
                base_url=base_url,
                timeout=timeout,
                headers=headers,
                follow_redirects=follow_redirects,
            )
            _clients[key] = c
        return c


class Borrowed:
    """共享客户端的「借用」视图。

    调用方大量使用 `with _client() as c:` 的写法，而共享客户端退出 `with` 时
    **不能**被关闭。这个视图把 `__enter__` 返回真正的共享客户端、`__exit__`
    什么都不做，其余属性原样委托——于是旧调用点无需改动即可安全复用连接池。
    """

    __slots__ = ("_c",)

    def __init__(self, client: httpx.Client) -> None:
        self._c = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)

    def __enter__(self) -> httpx.Client:
        return self._c

    def __exit__(self, *exc: Any) -> bool:
        return False  # 不关闭共享客户端


def borrow_client(
    key: str,
    *,
    base_url: str | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> Borrowed:
    """取共享客户端的借用视图（供 `with _client() as c:` 风格的调用点使用）。"""
    return Borrowed(
        get_client(
            key,
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            follow_redirects=follow_redirects,
        )
    )


def reset() -> None:
    """配置变更后调用：丢弃并关闭全部共享客户端（代理/URL/UA 可能已变）。"""
    with _lock:
        old = list(_clients.values())
        _clients.clear()
    for c in old:
        try:
            c.close()
        except Exception:
            pass


def stats() -> dict[str, Any]:
    """当前共享客户端数量（调试/测试用）。"""
    with _lock:
        return {"clients": sorted(_clients.keys())}
