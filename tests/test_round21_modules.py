"""第 21 轮 F7：media / library / fetcher / config / review_pipeline 缺陷回归。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_regressions import IsolatedTest  # noqa: E402
from backend import config, fetcher, httpc, library, media, memory  # noqa: E402


class _Body(httpx.SyncByteStream):
    """记录被读走多少字节的响应体。"""

    def __init__(self, total: int, chunk: int = 65536):
        self.total, self.chunk, self.read = total, chunk, 0

    def __iter__(self):
        while self.read < self.total:
            n = min(self.chunk, self.total - self.read)
            self.read += n
            yield b"a" * n


def _borrowed(handler):
    return httpc.Borrowed(httpx.Client(transport=httpx.MockTransport(handler)))


class MediaProbeTests(unittest.TestCase):
    def test_range_ignored_get_does_not_read_body(self):
        body = _Body(50 * 1024 * 1024)

        def handler(req):
            if req.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(200, headers={"content-type": "video/mp4",
                                                "content-length": str(body.total)}, stream=body)

        with patch.object(media, "_client", lambda t=None: _borrowed(handler)), \
                patch.object(httpc, "assert_public_url", lambda u: u):
            out = media.probe("https://cdn.test/abc")
        self.assertEqual(out["kind"], "video")
        self.assertEqual(out["size"], body.total)
        self.assertLess(body.read, 1024 * 1024)  # 旧实现会读满 50MB

    def test_206_size_from_content_range(self):
        def handler(req):
            if req.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(206, headers={"content-type": "audio/mpeg",
                                                "content-length": "1",
                                                "content-range": "bytes 0-0/12345"}, content=b"x")

        with patch.object(media, "_client", lambda t=None: _borrowed(handler)), \
                patch.object(httpc, "assert_public_url", lambda u: u):
            self.assertEqual(media.probe("https://cdn.test/a")["size"], 12345)

    def test_ytdlp_audio_removed_when_not_keeping_files(self):
        tmp = Path(self._tmpdir()) / "ytdl_x.m4a"
        tmp.write_bytes(b"audio")
        cfg = {"video_subtitles": False, "media_keep_files": False, "video_frames": 0}
        with patch.object(config, "get", side_effect=lambda k, d=None: cfg.get(k, d)), \
                patch.object(httpc, "assert_public_url", lambda u: u), \
                patch.object(media, "_ytdlp_info", return_value=None), \
                patch.object(media, "asr_available", return_value=(True, "small")), \
                patch.object(media, "_ytdlp_audio", return_value=str(tmp)), \
                patch.object(media, "transcribe", return_value={"ok": True, "text": "hi", "error": ""}):
            got = media.video_text("https://example.test/v")
        self.assertTrue(got["ok"])
        self.assertFalse(tmp.exists())

    def _tmpdir(self):
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name


class FetcherStreamTests(unittest.TestCase):
    def _fetch(self, handler, url="https://site.test/page"):
        with patch.object(fetcher, "_client", lambda t=None: _borrowed(handler)):
            return fetcher.fetch(url)

    def test_non_text_body_not_downloaded_before_dispatch(self):
        body = _Body(30 * 1024 * 1024)
        handler = lambda req: httpx.Response(  # noqa: E731
            200, headers={"content-type": "video/mp4"}, stream=body)
        with patch.object(media, "extract", return_value={"ok": True, "text": "t"}) as ex:
            got = self._fetch(handler)
        ex.assert_called_once()
        self.assertTrue(got["ok"])
        self.assertLess(body.read, 1024 * 1024)

    def test_html_read_is_capped(self):
        body = _Body(fetcher.MAX_HTML_BYTES * 3)
        handler = lambda req: httpx.Response(  # noqa: E731
            200, headers={"content-type": "text/html"}, stream=body)
        self._fetch(handler)
        self.assertLessEqual(body.read, fetcher.MAX_HTML_BYTES + 65536)

    def test_html_still_extracted(self):
        html = "<html><head><title>T</title></head><body><article>" + \
               "<p>正文内容很长。</p>" * 80 + "</article></body></html>"
        handler = lambda req: httpx.Response(  # noqa: E731
            200, headers={"content-type": "text/html; charset=utf-8"}, content=html.encode())
        got = self._fetch(handler)
        self.assertTrue(got["ok"], got["error"])
        self.assertIn("正文内容", got["text"])


class ConfigValidationTests(unittest.TestCase):
    def test_user_agent_control_chars_rejected(self):
        with self.assertRaises(ValueError):
            config._validated({"user_agent": "UA\r\nX-Evil: 1"})
        self.assertEqual(config._validated({"user_agent": "UA/1.0"}), {"user_agent": "UA/1.0"})

    def test_api_key_trailing_newline_tolerated_internal_rejected(self):
        self.assertIn("search_api_key", config._validated({"search_api_key": "abc\n"}))
        with self.assertRaises(ValueError):
            config._validated({"search_api_key": "ab\nc"})

    def test_new_ranges(self):
        for k, v in (("vector_ann_ef_search", 0), ("vector_ann_ef_search", 10 ** 9),
                     ("event_keep_days", -1), ("event_alert_threshold", 0)):
            with self.subTest(k=k, v=v), self.assertRaises(ValueError):
                config._validated({k: v})
        self.assertEqual(config._validated({"vector_ann_ef_search": 64}), {"vector_ann_ef_search": 64})


class LibraryTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        cache = patch.object(library, "TEXT_CACHE_DIR", Path(self.tmp.name) / "booktext")
        cache.start()
        self.addCleanup(cache.stop)
        library.init_db()
        self.book = Path(self.tmp.name) / "书.txt"
        self.book.write_text(("第一章 开始\n" + "这是一段用于测试导入流程的正文内容。" * 40 + "\n") * 2,
                             encoding="utf-8")
        c = memory.conn()
        c.execute("INSERT INTO books(id,path,format,title,status,created_at) VALUES(?,?,?,?,?,0)",
                  ("b1", str(self.book), "txt", "书", "pending"))
        c.commit()

    def _status(self):
        return dict(memory.conn().execute("SELECT * FROM books WHERE id='b1'").fetchone())

    def test_malformed_llm_output_does_not_crash(self):
        outputs = iter([["not", "dict"], {"points": ["string", 3]}, "x"] * 20)
        with patch.object(library.llm, "chat_json", side_effect=lambda *a, **k: next(outputs)):
            r = library.import_book("b1")
        self.assertTrue(r["ok"])
        self.assertEqual(self._status()["status"], "done")

    def test_exception_mid_import_does_not_leave_importing(self):
        with patch.object(library, "_extract_into_library", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                library.import_book("b1")
        self.assertEqual(self._status()["status"], "ready")

    def test_reimport_keeps_points_count(self):
        c = memory.conn()
        for i in range(3):
            c.execute("INSERT INTO knowledge(id,topic,claim,book_id) VALUES(?,?,?,?)",
                      (f"k{i}", "t", f"已存在的知识点内容 {i}", "b1"))
        c.commit()
        with patch.object(library.llm, "chat_json", return_value={"points": []}):
            r = library.import_book("b1")
        self.assertEqual(r["points"], 0)
        self.assertEqual(self._status()["points_count"], 3)

    def test_delete_book_purge_invalidates_caches(self):
        memory.conn().execute("INSERT INTO knowledge(id,topic,claim,book_id) VALUES('k1','t','x','b1')")
        memory.conn().commit()
        memory._search_cache_put("q", None, 5, [{"id": "k1"}])
        ver = memory.version()
        library.delete_book("b1", purge_points=True)
        self.assertIsNone(memory._search_cache_get("q", None, 5))
        self.assertGreater(memory.version(), ver)

    def test_import_url_stream_aborts_over_limit_and_cleans_part(self):
        body = _Body(3 * 1024 * 1024)
        real_client = httpx.Client

        def client(**kw):
            kw.pop("transport", None)
            kw.pop("event_hooks", None)
            return real_client(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, stream=body)), **kw)

        with patch.object(library, "MAX_URL_DOWNLOAD_BYTES", 1024 * 1024), \
                patch.object(httpc, "assert_public_url", lambda u: u), \
                patch.object(httpx, "Client", client):
            r = library.import_url("https://site.test/a:b.pdf")
        self.assertFalse(r["ok"])
        self.assertIn("200MB", r["msg"])
        self.assertLess(body.read, 2 * 1024 * 1024)
        lib = Path(self.tmp.name) / "library"
        self.assertEqual([p.name for p in lib.iterdir()] if lib.exists() else [], [])


if __name__ == "__main__":
    unittest.main()
