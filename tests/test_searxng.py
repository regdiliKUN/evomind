"""SearXNG 检索源（backend/search.py: search_searxng）测试，打桩 HTTP，不依赖本机容器。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from test_regressions import IsolatedTest

from backend import config, search


class SearxngTests(IsolatedTest):
    def _resp(self, results, code=200):
        r = MagicMock(status_code=code)
        r.json.return_value = {"results": results}
        return r

    def test_not_configured_returns_empty_without_request(self):
        with patch.object(search, "_searx_client") as mk:
            self.assertEqual(search.search_searxng("负形 构图"), [])
            mk.assert_not_called()

    def test_maps_results_dedups_and_sets_language(self):
        config.update({"searxng_url": "http://127.0.0.1:8888/"})
        client = MagicMock()
        client.get.return_value = self._resp([
            {"title": "A", "url": "https://a.com/1", "content": "x" * 900},
            {"title": "A2", "url": "https://a.com/1", "content": "dup"},
            {"title": "bad", "url": "javascript:alert(1)"},
            {"title": "B", "url": "https://b.com/2"},
        ])
        with patch.object(search, "_searx_client", return_value=client) as mk:
            out = search.search_searxng("负形 构图", 10)
        mk.assert_called_with("http://127.0.0.1:8888")
        self.assertEqual([r["url"] for r in out], ["https://a.com/1", "https://b.com/2"])
        self.assertEqual(out[0]["source"], "searxng")
        self.assertEqual(len(out[0]["snippet"]), 400)
        self.assertEqual(client.get.call_args.kwargs["params"]["language"], "zh-CN")
        with patch.object(search, "_searx_client", return_value=client):
            search.search_searxng("negative space", 10)
        self.assertEqual(client.get.call_args.kwargs["params"]["language"], "en")

    def test_errors_are_swallowed(self):
        config.update({"searxng_url": "http://127.0.0.1:8888"})
        client = MagicMock()
        client.get.side_effect = RuntimeError("down")
        with patch.object(search, "_searx_client", return_value=client):
            self.assertEqual(search.search_searxng("负形"), [])
        client.get.side_effect = None
        client.get.return_value = self._resp([], code=403)
        with patch.object(search, "_searx_client", return_value=client):
            self.assertEqual(search.search_searxng("负形"), [])

    def test_registered_and_url_validated(self):
        self.assertIn("searxng", search.DEFAULT_SOURCES)
        self.assertIs(search.SOURCES["searxng"], search.search_searxng)
        with self.assertRaises(ValueError):
            config.update({"searxng_url": "ftp://x"})
