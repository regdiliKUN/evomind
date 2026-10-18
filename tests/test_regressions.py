"""Offline regression tests; every test uses a disposable configuration and database.

Run: .venv/Scripts/python -m unittest discover -s tests -p test_*.py -v
"""
from __future__ import annotations

import concurrent.futures as cf
import gc
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import config, evolve, library, llm, memory


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="evomind_regression_")
        root = Path(self.tmp.name)
        self.paths = patch.multiple(
            config, DATA=root, LOGS=root, MODELS=root / "models",
            DATASETS=root / "datasets", KNOWLEDGE=root,
            CONFIG_FILE=root / "config.json", DB_FILE=root / "knowledge.db", _cache=None,
        )
        self.paths.start()
        self.local = patch.object(memory, "_local", threading.local())
        self.local.start()
        self.connections = []
        original_connect = memory.sqlite3.connect

        def connect(*args, **kwargs):
            c = original_connect(*args, **kwargs)
            self.connections.append(c)
            return c

        self.db_factory = patch.object(memory.sqlite3, "connect", side_effect=connect)
        self.db_factory.start()
        memory._invalidate()

    def tearDown(self):
        for c in self.connections:
            c.close()
        self.db_factory.stop()
        memory._invalidate()
        self.local.stop()
        self.paths.stop()
        gc.collect()
        self.tmp.cleanup()

    def point(self, i=0, topic="default", vec=None):
        return memory.add_point(
            topic=topic, claim=f"Regression knowledge point number {i}",
            vec=[1.0, 0.0] if vec is None else vec, dedup=False,
        )


class MemoryTests(IsolatedTest):
    def test_cache_reuses_normalized_vectors(self):
        self.point(vec=[3., 4.])
        memory.search("q", vec=[1., 0.])
        with patch.object(memory, "_normalize", wraps=memory._normalize) as normalize:
            memory.search("q", vec=[1., 0.])
        self.assertEqual(normalize.call_count, 1)  # only the query is normalized again

    def test_invalid_vectors_are_not_persisted(self):
        for vec in ([], [float("nan"), 1.], [float("inf"), 0.], [0., 0.]):
            with self.subTest(vec=vec), self.assertRaises(ValueError):
                self.point(vec=vec)
        self.assertEqual(memory.count_points(), 0)

    def test_requested_k_above_twenty(self):
        for i in range(60):
            self.point(i, vec=[1.0, i / 100])
        hits = memory.search("query", k=45, vec=[1.0, 0.0])
        self.assertEqual(len(hits), 45)
        self.assertEqual(hits[0]["claim"], "Regression knowledge point number 0")

    def test_topic_filter_applied_before_ranking(self):
        for i in range(25):
            self.point(i, topic="other", vec=[1.0, 0.0])
        pid = self.point(26, topic="wanted", vec=[0.7, 0.7])
        hits = memory.search("query", k=1, topic="wanted", vec=[1.0, 0.0])
        self.assertEqual([p["id"] for p in hits], [pid])

    def test_mixed_dimensions_and_corrupt_vectors(self):
        pid = self.point(1)
        other = self.point(2, vec=[0.0, 0.0, 1.0])
        bad = self.point(3)
        c = memory.conn()
        c.execute("UPDATE knowledge SET embedding=? WHERE id=?", (b"bad", bad))
        c.commit()
        memory._invalidate()
        self.assertEqual([p["id"] for p in memory.search("q", vec=[1., 0.])], [pid])
        self.assertEqual([p["id"] for p in memory.search("q", vec=[0., 0., 1.])], [other])

    def test_empty_queries_and_nonpositive_k_do_not_embed(self):
        with patch.object(llm, "embed", side_effect=AssertionError("unneeded inference")):
            self.assertEqual(memory.search(""), [])
            self.assertEqual(memory.search("q", k=0), [])
            self.assertEqual(memory.search("q", k=-1), [])

    def test_concurrent_dedup_is_atomic(self):
        config.load()
        barrier = threading.Barrier(2)
        original = memory.similarity

        def compare(*args, **kwargs):
            try:
                barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            return original(*args, **kwargs)

        def add(_):
            try:
                return memory.add_point(topic="race", claim="Identical concurrent knowledge",
                                        vec=[1., 0.])
            finally:
                memory.conn().close()

        with patch.object(memory, "similarity", side_effect=compare):
            with cf.ThreadPoolExecutor(max_workers=2) as pool:
                ids = list(pool.map(add, range(2)))
        self.assertEqual(sum(pid is not None for pid in ids), 1)

    def test_purge_book_invalidates_vector_cache(self):
        library.init_db()
        pid = self.point()
        c = memory.conn()
        c.execute("INSERT INTO books(id,path,title) VALUES ('book','missing.txt','Test')")
        c.execute("UPDATE knowledge SET book_id='book' WHERE id=?", (pid,))
        c.commit()
        memory.search("q", vec=[1., 0.])  # prime the cache before removing the book
        library.delete_book("book", purge_points=True)
        # The deleted vector must not suppress the same claim on re-import.
        added = memory.add_point(topic="test", claim="Reimport after book deletion", vec=[1., 0.])
        self.assertIsNotNone(added)
        self.assertEqual([p["id"] for p in memory.search("q", vec=[1., 0.])], [added])


class ConfigTests(IsolatedTest):
    def test_invalid_stored_value_falls_back_individually(self):
        config.CONFIG_FILE.write_text(json.dumps({"idle_minutes": "bad", "model": "custom"}),
                                      encoding="utf-8")
        self.assertEqual(config.get("idle_minutes"), config.DEFAULTS["idle_minutes"])
        self.assertEqual(config.get("model"), "custom")

    def test_load_returns_snapshot(self):
        cfg = config.load()
        cfg["model"] = "unpersisted mutation"
        self.assertEqual(config.get("model"), config.DEFAULTS["model"])

    def test_failed_atomic_save_preserves_file_and_cache(self):
        original = config.load()
        before = config.CONFIG_FILE.read_bytes()
        with patch.object(config.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                config.update({"model": "must not be saved"})
        self.assertEqual(config.CONFIG_FILE.read_bytes(), before)
        self.assertEqual(config.load(), original)
        self.assertEqual(list(config.DATA.glob("*.tmp")), [])

    def test_unknown_keys_ignored(self):
        config.update({"__unknown__": 5})
        self.assertIsNone(config.get("__unknown__"))

    def test_invalid_values_rejected_without_partial_update(self):
        original = config.load()
        for values in ({"fetch_per_round": 0}, {"idle_minutes": "bad"},
                       {"dedup_threshold": 1.1}, {"auto_download": "false"},
                       {"temperature": float("nan")}, {"model": None}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    config.update({"last_direction": "must not change", **values})
                self.assertEqual(config.load(), original)

    def test_parallel_updates_keep_all_fields(self):
        values = {"model": "test-model", "embed_model": "test-embed", "idle_minutes": 31,
                  "disk_budget_gb": 50, "max_rounds": 3, "last_direction": "test-direction"}
        config.load()
        with cf.ThreadPoolExecutor(max_workers=len(values)) as pool:
            list(pool.map(lambda item: config.update(dict([item])), values.items()))
        saved = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
        for k, v in values.items():
            self.assertEqual(saved[k], v)


class EngineTests(IsolatedTest):
    def test_invalid_rounds_do_not_create_session(self):
        for rounds in (0, -1, 101, "two", True):
            with self.subTest(rounds=rounds):
                self.assertFalse(evolve.Engine().start("test", rounds)["ok"])
        self.assertEqual(memory.list_sessions(), [])

    def test_thread_start_failure_releases_running_state(self):
        engine = evolve.Engine()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start")):
            with self.assertRaises(RuntimeError):
                engine.start("test", 1)
        self.assertFalse(engine.state["running"])
        self.assertEqual(memory.list_sessions()[0]["status"], "error")

    def test_ollama_unavailable_finishes_session(self):
        engine = evolve.Engine()
        sid = memory.start_session("test")
        engine.state["running"] = True
        with patch.object(llm, "healthy", return_value=False):
            engine._run("test", 1, sid)
        session = memory.list_sessions()[0]
        self.assertEqual(session["status"], "error")
        self.assertIsNotNone(session["ended_at"])
        self.assertFalse(engine.state["running"])

    def test_concurrent_start_reserves_one_session(self):
        engine = evolve.Engine()
        entered, release = threading.Event(), threading.Event()
        duplicate = threading.Event()
        results = []

        def create_session(_):
            if entered.is_set():
                duplicate.set()
            entered.set()
            release.wait(timeout=1)
            return "session"

        workers = [threading.Thread(target=lambda: results.append(engine.start("test", 1)))
                   for _ in range(2)]
        with patch.object(memory, "start_session", side_effect=create_session) as create:
            with patch.object(evolve.threading, "Thread"):
                workers[0].start()
                self.assertTrue(entered.wait(timeout=1))
                workers[1].start()
                duplicate.wait(timeout=0.2)
                release.set()
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(create.call_count, 1)


class LibraryTests(IsolatedTest):
    def test_add_directory_to_fresh_database(self):
        self.assertTrue(library.add_dir(self.tmp.name)["ok"])

    def test_book_source_link_committed_before_progress_callback(self):
        path = Path(self.tmp.name) / "book.txt"
        path.write_text("Useful source material for the test. " * 40, encoding="utf-8")
        library.init_db()
        c = memory.conn()
        c.execute("INSERT INTO books(id,path,title,format) VALUES ('book',?,'Test','txt')",
                  (str(path),))
        c.commit()
        links = []

        def progress(level, message, payload):
            if level == "learn":
                other = memory.sqlite3.connect(config.DB_FILE)
                try:
                    links.append(other.execute("SELECT book_id FROM knowledge").fetchone()[0])
                finally:
                    other.close()

        points = {"points": [{"claim": "A useful verified claim from the source",
                               "relevance": 1., "confidence": 0.9}]}
        with patch.object(llm, "chat_json", return_value=points):
            with patch.object(llm, "embed", return_value=[[1., 0.]]):
                result = library.import_book("book", max_chunks=1, on_event=progress)
        self.assertTrue(result["ok"])
        self.assertEqual(links, ["book"])


class ApiTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        from backend.main import app
        self.client = TestClient(app)  # omit startup: it warms external search services

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_invalid_config_returns_422(self):
        before = config.load()
        response = self.client.post("/api/config", json={"queries_per_round": -10})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(config.load(), before)

    def test_knowledge_topic_search_does_not_scan_disk(self):
        wanted = self.point(1, topic="wanted")
        self.point(2, topic="other")
        with patch.object(llm, "embed", return_value=[[1., 0.]]):
            with patch.object(config, "disk_usage_gb", side_effect=AssertionError("disk scan")):
                response = self.client.get("/api/knowledge", params={"q": "test", "topic": "wanted"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["id"] for p in response.json()["items"]], [wanted])
        self.assertEqual(response.json()["total"], 1)

    def test_negative_event_limit_is_bounded(self):
        memory.log_event("info", "test")
        response = self.client.get("/api/events/recent", params={"limit": -1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_static_console_served(self):
        for path in ("/", "/static/app.js", "/static/style.css"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
