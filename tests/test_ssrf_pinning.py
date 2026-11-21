"""DNS rebinding（TOCTOU）防护：连接层钉住已校验 IP。

对应 AUDIT_REVIEW.md §10 P1：`assert_public_url` 的预解析与 httpcore 自身的二次解析
之间存在间隙。这里用「第一次解析返回公网 IP、之后返回 127.0.0.1」的假 DNS 复现攻击：
旧路径（仅 request hook）会真的连上本机服务；钉 IP 后在建连那一刻被拒，本机服务收不到请求。
"""
from __future__ import annotations

import http.server
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpcore
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import httpc  # noqa: E402

_real_getaddrinfo = socket.getaddrinfo
PUBLIC_IP = "93.184.215.14"


def _is_literal(host) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except (OSError, TypeError):
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except (OSError, TypeError):
        return False


class RebindingDNS:
    """第一次查询 `evil.test` 返回公网 IP，之后都返回 127.0.0.1。IP 字面量走真实解析。"""

    def __init__(self, first: str = PUBLIC_IP, then: str = "127.0.0.1") -> None:
        self.first, self.then, self.calls = first, then, 0

    def __call__(self, host, port, *args, **kwargs):
        if _is_literal(host) or host == "localhost":
            return _real_getaddrinfo(host, port, *args, **kwargs)
        self.calls += 1
        ip = self.first if self.calls == 1 else self.then
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, int(port)))]


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.headers.get("Host", ""))
        if self.path.startswith("/start"):
            self.send_response(302)
            self.send_header("Location", f"http://evil.test:{self.server.server_address[1]}/meta")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"internal-secret"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默
        pass


class LocalServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.hits.clear()


class ResolvePublicTests(unittest.TestCase):
    def _fake(self, *ips):
        return lambda *a, **k: [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
            for ip in ips
        ]

    def test_all_public_dedup_keeps_order(self):
        with patch("socket.getaddrinfo", self._fake(PUBLIC_IP, "1.1.1.1", PUBLIC_IP)):
            self.assertEqual(httpc.resolve_public("x.test", 80), [PUBLIC_IP, "1.1.1.1"])

    def test_mixed_resolution_rejected(self):
        with patch("socket.getaddrinfo", self._fake(PUBLIC_IP, "10.0.0.5")):
            with self.assertRaises(httpc.PrivateAddressError):
                httpc.resolve_public("x.test", 80)

    def test_ipv4_mapped_loopback_rejected(self):
        with patch("socket.getaddrinfo", self._fake("::ffff:127.0.0.1")):
            with self.assertRaises(httpc.PrivateAddressError):
                httpc.resolve_public("x.test", 80)

    def test_metadata_and_multicast_rejected(self):
        for ip in ("169.254.169.254", "224.0.0.1", "0.0.0.0", "::1", "fe80::1"):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", self._fake(ip)):
                with self.assertRaises(httpc.PrivateAddressError):
                    httpc.resolve_public("x.test", 80)

    def test_unresolvable_is_plain_valueerror(self):
        def boom(*a, **k):
            raise socket.gaierror("nope")
        with patch("socket.getaddrinfo", boom):
            with self.assertRaises(ValueError) as cm:
                httpc.resolve_public("x.test", 80)
        self.assertNotIsInstance(cm.exception, httpc.PrivateAddressError)

    def test_assert_public_url_contract_unchanged(self):
        with patch("socket.getaddrinfo", self._fake("127.0.0.1")):
            with self.assertRaises(ValueError):
                httpc.assert_public_url("http://x.test/")
        with patch("socket.getaddrinfo", self._fake(PUBLIC_IP)):
            self.assertEqual(httpc.assert_public_url("http://x.test/a"), "http://x.test/a")
        with self.assertRaises(ValueError):
            httpc.assert_public_url("http://u:p@x.test/")


class RebindingTests(LocalServerCase):
    def _url(self):
        return f"http://evil.test:{self.port}/meta"

    def test_hook_only_client_is_vulnerable(self):
        """对照组：只有 request hook 时，rebinding 真的能打到本机服务（证明漏洞存在）。"""
        dns = RebindingDNS()
        with patch("socket.getaddrinfo", dns):
            c = httpc._build(event_hooks={"request": [httpc._public_url_hook]}, pin_public=False)
            try:
                r = c.get(self._url())
            finally:
                c.close()
        self.assertEqual(r.text, "internal-secret")
        self.assertEqual(len(_Handler.hits), 1)
        self.assertGreaterEqual(dns.calls, 2)  # hook 一次 + httpcore 自己一次

    def test_pinned_client_blocks_rebinding(self):
        dns = RebindingDNS()
        with patch("socket.getaddrinfo", dns), patch.object(httpc.config, "get", return_value=""):
            c = httpc._build(event_hooks={"request": [httpc._public_url_hook]}, pin_public=True)
            self.assertTrue(httpc.is_pinned(c))
            try:
                with self.assertRaises(httpc.PrivateAddressError):
                    c.get(self._url())
            finally:
                c.close()
        self.assertEqual(_Handler.hits, [])

    def test_pinned_connects_to_validated_ip_and_keeps_host_header(self):
        with patch.object(httpc, "resolve_public", return_value=["127.0.0.1"]) as rp, \
                patch.object(httpc.config, "get", return_value=""):
            c = httpc._build(pin_public=True)
            try:
                r = c.get(f"http://pinned.test:{self.port}/")
            finally:
                c.close()
        self.assertEqual(r.status_code, 200)
        rp.assert_called_with("pinned.test", self.port)
        self.assertEqual(_Handler.hits, [f"pinned.test:{self.port}"])

    def test_redirect_to_rebound_host_blocked_at_connect(self):
        """公网页面 302 到 rebinding 域名：重定向后的新连接同样在建连时被拒。

        start.test 视为「已校验的公网站点」（解析桩直接给出本机服务地址充当公网），
        它 302 到 evil.test；evil.test 走 rebinding DNS：hook 那次拿到公网 IP 放行，
        建连那次拿到 127.0.0.1 —— 必须在建连时被拒，本机 /meta 收不到请求。
        """
        dns = RebindingDNS()
        real_resolve = httpc.resolve_public

        def resolve(host, port):
            if host == "start.test":
                return ["127.0.0.1"]
            return real_resolve(host, port)

        with patch("socket.getaddrinfo", dns), patch.object(httpc, "resolve_public", resolve), \
                patch.object(httpc.config, "get", return_value=""):
            c = httpc._build(event_hooks={"request": [httpc._public_url_hook]}, pin_public=True)
            try:
                with self.assertRaises(httpc.PrivateAddressError):
                    c.get(f"http://start.test:{self.port}/start")
            finally:
                c.close()
        self.assertEqual(_Handler.hits, [f"start.test:{self.port}"])  # 只有起点被访问


class _FakeStream(httpcore.NetworkStream):
    def __init__(self, log):
        self.log = log

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.log.append(("tls", server_hostname))
        raise httpcore.ConnectError("stop-after-sni")

    def read(self, max_bytes, timeout=None):
        return b""

    def write(self, buffer, timeout=None):
        pass

    def close(self):
        pass


class _FakeInner(httpcore.NetworkBackend):
    def __init__(self, fail=()):
        self.log, self.fail = [], set(fail)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log.append(("tcp", host, port))
        if host in self.fail:
            raise httpcore.ConnectError(f"refused {host}")
        return _FakeStream(self.log)

    def sleep(self, seconds):
        pass


class BackendUnitTests(unittest.TestCase):
    def test_tls_sni_uses_domain_not_ip(self):
        inner = _FakeInner()
        pool = httpcore.ConnectionPool(network_backend=httpc.PublicOnlyBackend(inner))
        with patch.object(httpc, "resolve_public", return_value=[PUBLIC_IP]):
            with self.assertRaises(httpcore.ConnectError):
                pool.request("GET", "https://pinned.test/")
        pool.close()
        self.assertEqual(inner.log, [("tcp", PUBLIC_IP, 443), ("tls", "pinned.test")])

    def test_falls_back_to_next_validated_ip(self):
        inner = _FakeInner(fail={PUBLIC_IP})
        backend = httpc.PublicOnlyBackend(inner)
        with patch.object(httpc, "resolve_public", return_value=[PUBLIC_IP, "1.1.1.1"]):
            backend.connect_tcp("multi.test", 80)
        self.assertEqual([e[1] for e in inner.log if e[0] == "tcp"], [PUBLIC_IP, "1.1.1.1"])

    def test_all_ips_fail_raises_connect_error(self):
        inner = _FakeInner(fail={PUBLIC_IP})
        backend = httpc.PublicOnlyBackend(inner)
        with patch.object(httpc, "resolve_public", return_value=[PUBLIC_IP]):
            with self.assertRaises(httpcore.ConnectError):
                backend.connect_tcp("x.test", 80)

    def test_dns_failure_maps_to_connect_error(self):
        backend = httpc.PublicOnlyBackend(_FakeInner())

        def boom(*a, **k):
            raise socket.gaierror("nope")
        with patch("socket.getaddrinfo", boom):
            with self.assertRaises(httpcore.ConnectError):
                backend.connect_tcp("x.test", 80)

    def test_unix_socket_refused(self):
        with self.assertRaises(httpc.PrivateAddressError):
            httpc.PublicOnlyBackend(_FakeInner()).connect_unix_socket("/tmp/x.sock")


class SharedClientWiringTests(unittest.TestCase):
    def setUp(self):
        httpc.reset()

    def tearDown(self):
        httpc.reset()

    def _cfg(self, proxy):
        real = httpc.config.get
        return lambda k, d=None: proxy if k == "proxy" else real(k, d)

    def test_untrusted_keys_pinned_without_proxy(self):
        with patch.object(httpc.config, "get", self._cfg("")), \
                patch.object(httpc, "ALLOW_PRIVATE_URLS", False):
            for key in ("fetch", "media", "search"):
                self.assertTrue(httpc.is_pinned(httpc.get_client(key)), key)
            self.assertFalse(httpc.is_pinned(httpc.get_client("llm")))
            st = httpc.stats()
        self.assertEqual(st["pinned"], ["fetch", "media", "search"])

    def test_proxy_disables_pinning_but_keeps_hook(self):
        with patch.object(httpc.config, "get", self._cfg("http://127.0.0.1:7890")), \
                patch.object(httpc, "ALLOW_PRIVATE_URLS", False):
            c = httpc.get_client("fetch")
            self.assertFalse(httpc.is_pinned(c))
            self.assertIn(httpc._public_url_hook, c.event_hooks["request"])

    def test_allow_private_disables_pinning(self):
        with patch.object(httpc.config, "get", self._cfg("")), \
                patch.object(httpc, "ALLOW_PRIVATE_URLS", True):
            self.assertFalse(httpc.is_pinned(httpc.get_client("fetch")))
            self.assertIsNone(httpc.public_transport())

    def test_public_transport_for_standalone_clients(self):
        with patch.object(httpc, "ALLOW_PRIVATE_URLS", False):
            t = httpc.public_transport()
        self.assertIsNotNone(t)
        c = httpx.Client(transport=t)
        try:
            self.assertTrue(httpc.is_pinned(c))
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
