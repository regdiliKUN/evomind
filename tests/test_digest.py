"""关键词知识总结（backend/digest.py）验收测试。

全部用临时库 + 打桩模型，不依赖本机 Ollama。
运行：.venv/Scripts/python -m unittest discover -s tests -p test_digest.py
"""
from __future__ import annotations

import json
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_regressions import IsolatedTest

from backend import digest, memory


def _add(claim, status, vec, topic="构图", detail=""):
    res = memory.add_points_batch([{"topic": topic, "claim": claim, "detail": detail, "vec": vec}], dedup=False)
    pid = res["ids"][0]
    c = memory.conn()
    c.execute("UPDATE knowledge SET status=? WHERE id=?", (status, pid))
    c.commit()
    memory._invalidate()
    return pid


def _fake_json(system, user, **kw):
    if "术语专家" in system:
        return {"meaning": "构图中的负空间", "terms": ["负空间", "图底关系"]}
    # 筛选：凡是含「负值」的都判为字面近亲，排除
    lines = [ln for ln in user.splitlines() if ln.startswith("[")]
    keep = [i for i, ln in enumerate(lines) if "负值" not in ln]
    return {"relevant": keep}


class DigestTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.a = _add("负形是画面中主体之外的空白区域，同样构成形状。", "verified", [1., 0., 0.])
        self.b = _add("利用负空间可以让剪影更易识别，图底关系要清晰。", "verified", [.9, .1, 0.])
        self.c = _add("Lambda Factor 取负值会增强网格形状。", "verified", [.8, .2, 0.], topic="ZBrush")
        self.d = _add("负形在历史资料里也被称为留白。", "legacy", [.95, .05, 0.])
        self.e = _add("负形归档后的旧说法不该出现。", "archived", [1., 0., 0.])
        self.embed = patch("backend.llm.embed", side_effect=lambda texts, **k: [[1., 0., 0.] for _ in texts])
        self.embed.start()

    def tearDown(self):
        self.embed.stop()
        super().tearDown()

    def _run(self, **kw):
        with patch("backend.llm.chat_json", side_effect=_fake_json), \
             patch("backend.llm.chat", return_value="## 一句话概括\n负形即主体以外的形状 [1][2]"):
            return list(digest.run("负形", **kw))

    def test_verified_only_by_default_and_screens_out_lookalikes(self):
        evts = self._run()
        done = evts[-1]
        self.assertEqual(done["stage"], "done", evts)
        ids = [p["id"] for p in done["digest"]["points"]]
        self.assertIn(self.a, ids)
        self.assertIn(self.b, ids)
        self.assertNotIn(self.c, ids, "字面近亲应被筛选剔除")
        self.assertNotIn(self.d, ids, "默认只用已证实知识")
        self.assertNotIn(self.e, ids)
        self.assertEqual(done["digest"]["terms"], ["负形", "负空间", "图底关系"])
        # 字面命中排在前面
        self.assertTrue(done["digest"]["points"][0]["literal"])

    def test_include_legacy_widens_scope_but_never_archived(self):
        ids = [p["id"] for p in self._run(include_legacy=True)[-1]["digest"]["points"]]
        self.assertIn(self.d, ids)
        self.assertNotIn(self.e, ids)

    def test_failed_screen_batch_keeps_only_literal_hits(self):
        def flaky(system, user, **kw):
            if "术语专家" in system:
                return {"terms": []}
            raise RuntimeError("model down")
        with patch("backend.llm.chat_json", side_effect=flaky), patch("backend.llm.chat", return_value="ok [1]"):
            done = list(digest.run("负形"))[-1]
        ids = [p["id"] for p in done["digest"]["points"]]
        self.assertEqual(ids, [self.a])
        self.assertEqual(done["digest"]["stats"]["failed_batches"], 1)

    def test_nothing_found_reports_empty(self):
        with patch("backend.llm.chat_json", return_value={"terms": []}), \
             patch("backend.memory.search", return_value=[]):
            evts = list(digest.run("量子纠缠"))
        self.assertEqual(evts[-1]["stage"], "empty")
        self.assertEqual(evts[-1]["progress"], 1.0)

    def test_all_rejected_reports_empty_not_error(self):
        # 模型判定候选全部不相关是正常结局：进度要收满，并说明字面命中也被排除了
        def reject_all(system, user, **kw):
            return {"relevant": []} if "候选知识" in user else {"terms": []}
        with patch("backend.llm.chat_json", side_effect=reject_all):
            evts = list(digest.run("负形"))
        self.assertEqual(evts[-1]["stage"], "empty")
        self.assertEqual(evts[-1]["progress"], 1.0)
        self.assertIn("字面含检索词", evts[-1]["msg"])

    def test_saved_history_markdown_and_delete_via_api(self):
        from backend.main import app
        cli = TestClient(app)
        with patch("backend.llm.chat_json", side_effect=_fake_json), \
             patch("backend.llm.chat", return_value="## 一句话概括\n内容 [1]"):
            r = cli.post("/api/digest/stream", json={"keyword": "负形"})
        evts = [json.loads(ln[5:]) for ln in r.text.split("\n\n") if ln.startswith("data:")]
        did = evts[-1]["digest"]["id"]
        items = cli.get("/api/digests").json()["items"]
        self.assertEqual(items[0]["id"], did)
        self.assertEqual(items[0]["count"], 2)
        md = cli.get(f"/api/digests/{did}/markdown")
        self.assertEqual(md.status_code, 200)
        self.assertIn("「负形」知识总结", md.text)
        self.assertIn("引用的知识条目", md.text)
        self.assertTrue(cli.delete(f"/api/digests/{did}").json()["ok"])
        self.assertEqual(cli.get(f"/api/digests/{did}").status_code, 404)

    def test_concurrent_run_is_rejected(self):
        self.assertTrue(digest._run_lock.acquire(blocking=False))
        try:
            evts = list(digest.run("负形"))
        finally:
            digest._run_lock.release()
        self.assertEqual(evts[-1]["stage"], "error")
        self.assertIn("进行中", evts[-1]["msg"])
