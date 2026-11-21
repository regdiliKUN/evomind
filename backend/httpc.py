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

import ipaddress
import os
import socket
import threading
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from . import config

_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}

#: A/B 基准开关。置 True 时 `get_client` 每次返回全新客户端（复刻优化前行为），
#: 关闭即恢复共享。仅供 scripts/bench 使用，正常流程恒为 False。
FORCE_NEW_CLIENT = False

#: 允许访问内网地址（自建内网服务调试用），默认关闭。公网抓取通道一旦被
#: 诱导访问 127.0.0.1 或 169.254.169.254，就是典型 SSRF。
ALLOW_PRIVATE_URLS = os.environ.get("EVOMIND_ALLOW_PRIVATE_URLS") == "1"

#: 这些 key 的客户端会拿外部提供的 URL 发请求，需要逐请求校验（含重定向）。
#: "llm" 不在其中：它本来就要访问本机 Ollama。
UNTRUSTED_KEYS = frozenset({"fetch", "media", "search"})


def assert_url_shape(url: str) -> str:
    """只做不依赖网络的结构校验：协议、内嵌凭据、主机名。

    DNS 解析与 IP 判定留给 `assert_public_url`——它在请求真正发出前由
    request hook 调用。这样「DNS 暂时不可用」不会被误判成「恶意地址」，
    同一 URL 也不会在入口 / probe / hook 处被重复解析多次。
    """
    u = urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 链接")
    if u.username or u.password:
        raise ValueError("URL 不允许携带凭据")
    if not u.hostname:
        raise ValueError("URL 缺少主机名")
    return url


class PrivateAddressError(ValueError):
    """目标解析到了内网 / 回环 / 保留地址。继承 ValueError，调用方既有的捕获不变。"""


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # ::ffff:127.0.0.1 这类 IPv4 映射地址按其承载的 IPv4 判定，
    # 不依赖不同 Python 小版本对 is_global 的口径差异。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int) -> list[str]:
    """解析主机并要求**全部**结果都是公网地址，返回去重后的 IP 列表（保持解析顺序）。

    任一结果落在内网 / 回环 / 保留段即整体拒绝（与 `assert_public_url` 口径一致：
    混合解析结果是 DNS rebinding 的常见手法）。解析不出合法地址视为解析失败。
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ValueError(f"无法解析主机：{host}") from e
    out: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            # 解析不了的条目不能被「跳过后再连」，直接不纳入候选。
            continue
        if not _is_public_ip(ip):
            raise PrivateAddressError("不允许访问内网 / 回环 / 保留地址")
        if raw not in out:
            out.append(raw)
    if not out:
        raise ValueError(f"无法解析主机：{host}")
    return out


def assert_public_url(url: str) -> str:
    """校验 URL 指向公网地址，拒绝回环 / 私网 / 链路本地 / 保留段与凭据注入。"""
    assert_url_shape(url)
    u = urlsplit(url)
    if ALLOW_PRIVATE_URLS:
        return url
    resolve_public(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    return url


class PublicOnlyBackend(httpcore.NetworkBackend):
    """连接层的 SSRF 防线：在**真正建 TCP 连接的那一刻**解析、校验并直连已校验 IP。

    只靠 request hook 时存在 DNS rebinding 的 TOCTOU：hook 解析到公网 IP 放行，
    httpcore 随后自己再解析一次，攻击者的 DNS 在两次解析之间把记录切到
    127.0.0.1 / 169.254.169.254 即可绕过。这里把「校验的 IP」和「连接的 IP」
    变成同一个值，间隙消失。

    TLS 不受影响：httpcore 在 `connect_tcp` 之后用 origin 的主机名做 `start_tls`
    （SNI 与证书校验仍按域名），Host 头由 httpx 按 URL 生成，同样不变。
    keep-alive 复用的连接在建立时已校验过，重定向到新主机会新建连接再次校验。
    """

    def __init__(self, inner: httpcore.NetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        try:
            ips = resolve_public(host, port)
        except PrivateAddressError:
            raise
        except ValueError as e:
            # DNS 失败按连接失败上报，与 httpcore 默认后端的异常类型一致。
            raise httpcore.ConnectError(str(e)) from e
        last: Exception | None = None
        for ip in ips:  # 与 socket.create_connection 一样逐个地址尝试
            try:
                return self._inner.connect_tcp(
                    ip, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        assert last is not None
        raise last

    def connect_unix_socket(self, path: str, timeout: float | None = None,
                            socket_options: Any = None) -> httpcore.NetworkStream:
        raise PrivateAddressError("公网通道不允许 Unix socket 连接")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def _pinned_transport() -> httpx.HTTPTransport | None:
    """直连（无代理）时使用的钉 IP 传输层；httpx 内部结构不符时返回 None 退回仅 hook 防护。

    走代理时目标域名由代理解析，本机无从钉住，保留 request hook 的预校验。
    """
    t = httpx.HTTPTransport(trust_env=False)
    pool = getattr(t, "_pool", None)
    if type(pool) is not httpcore.ConnectionPool:
        return None
    t._pool = httpcore.ConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        max_connections=pool._max_connections,
        max_keepalive_connections=pool._max_keepalive_connections,
        keepalive_expiry=pool._keepalive_expiry,
        network_backend=PublicOnlyBackend(),
    )
    return t


def public_transport() -> httpx.HTTPTransport | None:
    """供独立 httpx.Client（不走代理时）复用的钉 IP 传输层；放行内网时返回 None。"""
    if ALLOW_PRIVATE_URLS:
        return None
    return _pinned_transport()


def _public_url_hook(request: "httpx.Request") -> None:
    """挂在共享客户端上：每个请求（含重定向后的请求）发出前都校验一次。"""
    assert_public_url(str(request.url))


def _hooks_for(key: str) -> dict | None:
    if key in UNTRUSTED_KEYS and not ALLOW_PRIVATE_URLS:
        return {"request": [_public_url_hook]}
    return None


def public_url_hooks() -> dict | None:
    """供独立 httpx.Client 复用：把 SSRF 校验覆盖到重定向后的每次实际请求。"""
    return _hooks_for("fetch")


def _build(
    *,
    base_url: str | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
    event_hooks: dict | None = None,
    pin_public: bool = False,
) -> httpx.Client:
    # base_url 为 None 时不能传该参数（httpx 会抛 TypeError）
    kw: dict[str, Any] = {}
    if base_url:
        kw["base_url"] = base_url
    proxy = config.get("proxy") or None
    if pin_public and not proxy and not ALLOW_PRIVATE_URLS:
        transport = _pinned_transport()
        if transport is not None:
            kw["transport"] = transport
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers=headers,
        follow_redirects=follow_redirects,
        proxy=proxy,
        trust_env=False,
        event_hooks=event_hooks,
        **kw,
    )


def is_pinned(client: httpx.Client) -> bool:
    """客户端的直连传输层是否已钉住公网 IP（测试 / 状态面板用）。"""
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    return isinstance(getattr(pool, "_network_backend", None), PublicOnlyBackend)


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
            event_hooks=_hooks_for(key),
            pin_public=key in UNTRUSTED_KEYS,
        )
    with _lock:
        c = _clients.get(key)
        if c is None or c.is_closed:
            c = _build(
                base_url=base_url,
                timeout=timeout,
                headers=headers,
                follow_redirects=follow_redirects,
                event_hooks=_hooks_for(key),
                pin_public=key in UNTRUSTED_KEYS,
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
    """配置变更后调用：丢弃并关闭全部共享客户端（代理/URL/UA 可能已变）。

    close() 必须留在锁内：否则另一线程可能刚取到旧客户端、下一刻它就被关掉，
    请求报 `Client is closed`。close 很快，持锁代价可忽略。
    """
    with _lock:
        old = list(_clients.values())
        _clients.clear()
        for c in old:
            try:
                c.close()
            except Exception:  # noqa: BLE001  关闭失败不影响重建
                pass


def stats() -> dict[str, Any]:
    """当前共享客户端数量（调试/测试用）。"""
    with _lock:
        return {
            "clients": sorted(_clients.keys()),
            "pinned": sorted(k for k, c in _clients.items() if is_pinned(c)),
        }
