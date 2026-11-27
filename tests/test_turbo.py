"""狂暴模式（backend/turbo.py）验收测试。

临时配置目录 + 打桩，不依赖 Ollama，不真的改系统优先级。
运行：.venv/Scripts/python -m unittest discover -s tests -p test_turbo.py
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from test_regressions import IsolatedTest

from backend import config, llm, scheduler, turbo


class TurboTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.prio = patch.object(turbo, "_set_priority", return_value=["EvoMind"])
        self.prio_mock = self.prio.start()
        # 后台 OCR 线程另有专门用例；这里只测开关本身，不让它真的起线程
        self.ocr_start = patch("backend.cpu_ocr.start")
        self.ocr_stop = patch("backend.cpu_ocr.stop")
        self.ocr_start_mock = self.ocr_start.start()
        self.ocr_stop.start()

    def tearDown(self):
        turbo.set_enabled(False)
        self.ocr_start.stop()
        self.ocr_stop.stop()
        self.prio.stop()
        super().tearDown()

    def test_default_off_keeps_original_numbers(self):
        self.assertFalse(turbo.on())
        self.assertEqual(turbo.workers("extract", 2), 2)
        self.assertEqual(turbo.workers("fetch", 4), 4)
        self.assertEqual(turbo.per_round("queries_per_round", 4), 4)
        self.assertEqual(scheduler.interval(), 15)
        self.assertIsNone(turbo.keep_alive())

    def test_on_boosts_and_caps(self):
        config.update({"fetch_per_round": 40, "queries_per_round": 4})
        turbo.set_enabled(True)
        self.assertEqual(turbo.workers("extract", 2), 4)
        self.assertEqual(turbo.workers("fetch", 4), 12)
        self.assertEqual(turbo.per_round("queries_per_round", 4), 4)  # 检索不放大，免得被限流
        self.assertEqual(turbo.workers("search", 4), 4)
        self.assertEqual(turbo.per_round("fetch_per_round", 8), 50)  # 封顶，不超配置上限
        self.assertEqual(scheduler.interval(), 1)
        self.assertEqual(turbo.keep_alive(), -1)

    def test_keep_awake_thread_follows_switch(self):
        turbo.set_enabled(True)
        import os
        if os.name == "nt":
            self.assertTrue(turbo.state()["keep_awake"])
        turbo.set_enabled(False)
        self.assertFalse(turbo.state()["keep_awake"])
        self.prio_mock.assert_called_with(False)

    def test_llm_payload_gets_keep_alive(self):
        resp = MagicMock()
        resp.json.return_value = {"message": {"content": "ok"}}
        client = MagicMock()
        client.post.return_value = resp
        with patch.object(llm, "_client", return_value=client):
            llm.chat([{"role": "user", "content": "hi"}])
            self.assertNotIn("keep_alive", client.post.call_args.kwargs["json"])
            turbo.set_enabled(True)
            llm.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(client.post.call_args.kwargs["json"]["keep_alive"], -1)

    def test_api_toggle(self):
        from backend.main import app

        c = TestClient(app)
        self.assertFalse(c.get("/api/turbo").json()["enabled"])
        r = c.post("/api/turbo", json={"enabled": True}).json()
        self.assertTrue(r["enabled"])
        self.assertEqual(r["workers"]["fetch"], 12)
        self.assertTrue(config.get("turbo_mode"))
        self.assertEqual(c.post("/api/turbo", json={"enabled": "yes"}).status_code, 422)
        self.assertFalse(c.post("/api/turbo", json={"enabled": False}).json()["enabled"])
