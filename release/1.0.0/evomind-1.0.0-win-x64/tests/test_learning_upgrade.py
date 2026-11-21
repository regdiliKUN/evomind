"""Learning pipeline regressions, using disposable data and deterministic providers."""
import base64
import json
import threading
import concurrent.futures
from unittest.mock import MagicMock, patch

from test_regressions import IsolatedTest
from backend import config, directions, evolve, finetune, library, llm, memory, refinement, scheduler, search, tasks
from fastapi.testclient import TestClient


class LearningUpgradeTests(IsolatedTest):
    def test_launcher_checks_live_health_before_spawning_ollama(self):
        import run as launcher
        with patch.object(llm, "tags", return_value=(True, [])) as tags, \
             patch.object(launcher, "find_ollama_exe", side_effect=AssertionError("already online")), \
             patch.object(launcher, "log"):
            self.assertTrue(launcher.ensure_ollama())
        tags.assert_called_once_with(fresh=True)

    def test_legacy_interval_never_exceeds_thirty_seconds(self):
        config.update({"last_direction": "ZBrush", "idle_minutes": 20})
        with patch.dict(scheduler._state, enabled=True, last_run=100., next_run=0.), \
             patch.object(scheduler.time, "time", return_value=131.), \
             patch.object(evolve.engine, "start", return_value={"ok": True}) as start, \
             patch.dict(evolve.engine.state, running=False):
            scheduler._tick()
            start.assert_called_once()

    def test_bing_tracking_link_is_readable(self):
        url = "https://help.maxon.net/zbr/en-us/Content/DynaMesh.html"
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.status_code = 200
        client.get.return_value.text = (
            '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1'
            + encoded + '">DynaMesh documentation</a></h2><p>Resolution details</p></li>'
        )
        with patch.object(search, "_client", return_value=client):
            results = search.search_bing("ZBrush DynaMesh")
        self.assertEqual([r["url"] for r in results], [url])

    def test_successful_fetch_does_not_complete_learning(self):
        engine = evolve.Engine()
        doc = {"ok": True, "url": "https://example.org/lesson", "title": "lesson", "text": "source " * 100}
        with patch.object(evolve.fetcher, "fetch", return_value=doc):
            engine._fetch_all([{"url": doc["url"]}])
        self.assertFalse(memory.doc_seen(doc["url"]))

    def test_interval_is_measured_from_completion(self):
        config.update({"last_direction": "ZBrush", "idle_seconds": 30})
        with patch.dict(scheduler._state, enabled=True, last_run=100., next_run=0.), \
             patch.dict(evolve.engine.state, running=False, finished_at=300.), \
             patch.object(scheduler.time, "time", return_value=329.), \
             patch.object(evolve.engine, "start", return_value={"ok": True}) as start:
            scheduler._tick()
            start.assert_not_called()
            self.assertEqual(scheduler.state()["next_run"], 330.)
            with patch.object(scheduler.time, "time", return_value=330.):
                scheduler._tick()
            start.assert_called_once_with("ZBrush", 2)

    def test_scheduler_waits_for_other_heavy_tasks(self):
        config.update({"last_direction": "ZBrush"})
        with patch.dict(scheduler._state, enabled=True), \
             patch.dict(refinement._state, running=True), \
             patch.object(evolve.engine, "start") as start:
            scheduler._tick()
            start.assert_not_called()
            self.assertIn("炼化", scheduler.state()["waiting"])

    def test_stop_clears_auto_deadline_and_does_not_restart(self):
        from backend.main import api_learn_stop
        with patch.dict(scheduler._state, enabled=True, next_run=123.), \
             patch.object(evolve.engine, "stop", return_value={"ok": True}):
            api_learn_stop()
            self.assertFalse(scheduler.state()["enabled"])
            self.assertEqual(scheduler.state()["next_run"], 0)
            self.assertFalse(config.get("continuous_learning"))

    def test_old_config_migrates_without_losing_settings(self):
        config.CONFIG_FILE.write_text(json.dumps({"idle_minutes": 20, "model": "my-model"}), encoding="utf-8")
        self.assertEqual(config.get("idle_seconds"), 30)
        self.assertEqual(config.get("model"), "my-model")

    def test_new_query_history_survives_engine_instances(self):
        memory.remember_queries("ZBrush", ["ZBrush DynaMesh resolution"])
        self.assertEqual(memory.recent_queries("ZBrush"), ["ZBrush DynaMesh resolution"])
        self.assertEqual(memory.recent_queries("Blender"), [])

    def test_document_completion_is_scoped_to_topic(self):
        memory.mark_learned("https://test.local", "ZBrush")
        self.assertTrue(memory.doc_seen("https://test.local", "ZBrush"))
        self.assertFalse(memory.doc_seen("https://test.local", "Blender"))

    def test_general_search_runs_even_when_specialized_source_has_hits(self):
        item = {"title": "ZBrush DynaMesh", "url": "https://example.org/page", "snippet": "resolution"}
        wiki, bing = MagicMock(return_value=[item]), MagicMock(return_value=[])
        with patch.dict(search.SOURCES, wiki=wiki, bing=bing):
            self.assertTrue(search.search("ZBrush DynaMesh", sources=["wiki", "bing"]))
        wiki.assert_called_once()
        bing.assert_called_once()

    def test_tracking_urls_dedupe_and_unrelated_product_is_rejected(self):
        a = {"url": "https://help.maxon.net/doc?utm_source=bing#part", "title": "ZBrush DynaMesh", "snippet": "resolution"}
        b = dict(a, url="https://help.maxon.net/doc")
        bad = {"url": "https://other.org", "title": "Blender resolution", "snippet": ""}
        self.assertEqual(len(search.dedupe([a, b])), 1)
        self.assertEqual([r["title"] for r in search.rank_results([bad, a], "ZBrush resolution")], [a["title"]])
        self.assertTrue(search.should_use_arxiv("RAG LLM retrieval"))

    def test_official_docs_index_works_without_search_engines(self):
        root = search.DOC_ROOTS["zbrush"]
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.status_code = 200
        client.get.return_value.text = '<a href="methods/methods.html">DynaMesh Methods</a><a href="https://bad.org/">external</a>'
        with patch.dict(search._docs_cache, clear=True), patch.object(search, "_client", return_value=client):
            result = search.search_documentation("ZBrush DynaMesh")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "official_docs")
        self.assertTrue(result[0]["url"].startswith("https://help.maxon.net/"))

    def test_direction_deck_has_six_unique_fresh_choices(self):
        first = directions.refresh("ZBrush")["items"]
        second = directions.refresh("ZBrush", first)["items"]
        self.assertEqual(len(set(first)), 6)
        self.assertFalse(set(first) & set(second))
        self.assertTrue(all(s.startswith("ZBrush：") for s in first + second))
        self.assertEqual(len(directions.refresh("角色雕刻")["items"]), 6)

    def test_extraction_requires_real_evidence_and_retries_failure(self):
        engine = evolve.Engine()
        doc = {"url": "https://test.local/guide", "title": "Guide", "text": "DynaMesh resolution controls polygon density. " * 8}
        point = {"claim": "DynaMesh 分辨率会控制生成网格的多边形密度", "detail": "提高分辨率增加多边形。", "relevance": .9, "confidence": .8}
        with patch.object(llm, "chat_json", return_value={"points": [point]}):
            self.assertEqual(engine._extract_all([doc], "ZBrush", ""), [])
        self.assertFalse(memory.doc_seen(doc["url"]))
        point["evidence"] = "DynaMesh resolution controls polygon density."
        with patch.object(llm, "chat_json", side_effect=[{"points": [point]}, {"verified": [0]}]), \
             patch.object(llm, "embed", return_value=[[1., 0.]]):
            result = engine._extract_all([doc], "ZBrush", "")
        self.assertEqual(len(result), 1)
        self.assertTrue(memory.doc_seen(doc["url"], "ZBrush"))
        self.assertIn(point["evidence"], memory.list_points()[0]["detail"])

    def test_verification_discards_unsupported_claims_and_invalid_indices(self):
        points = [{"claim": "Supported claim", "detail": "detail\n原文依据：source"},
                  {"claim": "Unsupported claim", "detail": "detail\n原文依据：source"}]
        with patch.object(llm, "chat_json", return_value={"verified": [0, 999, True, "1"]}):
            self.assertEqual(evolve.Engine()._verify_points(points), [points[0]])

    def test_refinement_preserves_originals_and_persists_citations(self):
        p1, p2 = self.point(1, topic="ZBrush"), self.point(2, topic="ZBrush")
        rows = memory.list_points("ZBrush")
        out = {"items": [{"claim": "Core knowledge summary", "detail": "", "source_ids": [p1, p2]}]}
        with patch.object(llm, "chat_json", side_effect=[out, {"verified": [0]}]), patch.dict(refinement._state, saved=0, done=0, stage="refining", error=""):
            refinement._stop.clear()
            refinement._run(rows)
            self.assertEqual(refinement.state()["stage"], "done")
            self.assertEqual(refinement.state()["saved"], 1)
        self.assertEqual(memory.count_points(), 2)
        saved = refinement.history("ZBrush")[0]
        self.assertEqual(set(saved["items"][0]["source_ids"]), {p1, p2})
        self.assertLess(saved["after_chars"], saved["before_chars"])

    def test_refinement_rejects_invented_citations_and_unsupported_schema(self):
        rows = [{"id": "valid", "claim": "original"}]
        for data in ([], {"items": [{"claim": "A fabricated core", "source_ids": ["not-real"]}]}):
            with self.subTest(data=data), patch.object(llm, "chat_json", return_value=data), self.assertRaises(ValueError):
                refinement._distill(rows, {"valid": rows[0]})

    def test_heavy_task_admission_reservation_prevents_races(self):
        entered, release = threading.Event(), threading.Event()
        @tasks.exclusive("test-worker")
        def work():
            entered.set()
            release.wait(2)
            return {"ok": True}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(work)
            self.assertTrue(entered.wait(1))
            self.assertFalse(work()["ok"])
            release.set()
            self.assertTrue(first.result()["ok"])
        self.assertFalse(tasks._pending)

    def test_library_reserves_before_thread_can_begin(self):
        with patch.object(library.threading, "Thread"), patch.dict(library._task, running=False):
            self.assertTrue(library.import_books_async(["test"])["ok"])
            self.assertTrue(library.state()["running"])
            self.assertFalse(evolve.engine.start("ZBrush")["ok"])

    def test_training_reserves_before_thread_and_handles_spawn_failure(self):
        with patch.dict(finetune._state, running=False), patch.object(finetune.threading, "Thread") as thread:
            self.assertTrue(finetune.train_async("test.json")["ok"])
            self.assertTrue(finetune.state()["running"])
        with patch.dict(finetune._state, running=False), patch.object(finetune.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("spawn failed")
            with self.assertRaises(RuntimeError):
                finetune.train_async("test.json")
            self.assertFalse(finetune.state()["running"])

    def test_new_api_validation_and_rejected_start_keep_direction(self):
        from backend.main import app
        client = TestClient(app)  # omit startup: no real network or background scheduler
        try:
            for url, body in (
                ("/api/learn/start", {"direction": [], "rounds": 2}),
                ("/api/learn/start", {"direction": "ZBrush", "rounds": True}),
                ("/api/auto", {"enabled": "false"}),
                ("/api/auto", {"enabled": True, "idle_seconds": 31}),
                ("/api/learn/directions", {"seed": " "}),
                ("/api/refinements/start", {"topic": {}}),
            ):
                self.assertEqual(client.post(url, json=body).status_code, 422)
            self.assertEqual(len(client.post("/api/learn/directions", json={"seed": "ZBrush"}).json()["items"]), 6)
            config.update({"last_direction": "old direction"})
            with patch.object(evolve.engine, "start", return_value={"ok": False, "msg": "busy"}):
                self.assertFalse(client.post("/api/learn/start", json={"direction": "new direction"}).json()["ok"])
            self.assertEqual(config.get("last_direction"), "old direction")
            self.assertEqual(client.get("/api/refinements").json()["items"], [])
            self.point(1, topic="old topic")
            self.point(2, topic="new topic")
            knowledge = client.get("/api/knowledge?limit=1").json()
            self.assertEqual(len(knowledge["items"]), 1)
            self.assertEqual({t["topic"] for t in knowledge["topics"]}, {"old topic", "new topic"})
        finally:
            client.close()
