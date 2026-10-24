"""Reversible duplicate cleanup preserves distinct facts, provenance, and references."""
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_regressions import IsolatedTest
from backend import cleanup, evolve, memory


class CleanupTests(IsolatedTest):
    def add(self, claim="DynaMesh controls polygon density.", detail="Resolution controls the density.", confidence=.6, topic="ZBrush", book=""):
        return memory.add_point(topic=topic, claim=claim, detail=detail, confidence=confidence,
                                source_url=f"file:///{book or 'source'}.txt", source_title=book or "Source",
                                book_id=book, vec=[1., 0.], dedup=False)

    def test_cleanup_keeps_best_fact_and_preserves_all_originals_for_undo(self):
        first = self.add(book="Book A")
        best = self.add(confidence=.9, book="Book B")
        original = {r["id"]: dict(r) for r in memory.conn().execute("SELECT * FROM knowledge")}
        result = cleanup.clean()
        self.assertEqual((result["removed"], result["remaining"]), (1, 1))
        self.assertEqual(memory.list_points()[0]["id"], best)
        self.assertEqual(memory.get_point(best)["duplicate_sources"][0]["source_title"], "Book A")
        archived = memory.get_point(first)
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["canonical_id"], best)
        self.assertEqual(archived["source_title"], "Book A")
        self.assertEqual(cleanup.undo()["restored"], 1)
        restored = {r["id"]: dict(r) for r in memory.conn().execute("SELECT * FROM knowledge")}
        self.assertEqual(original, restored)
        self.assertFalse(cleanup.state()["available"])

    def test_different_details_numbers_negations_code_and_topics_survive(self):
        variants = [
            ("ZBrush requires memory", "Requires 8 GB RAM", "ZBrush"),
            ("ZBrush requires memory", "Requires 16 GB RAM", "ZBrush"),
            ("ZBrush requires memory", "Does not require 8 GB RAM", "ZBrush"),
            ("ZBrush requires memory", "Requires 8 GB RAM", "Blender"),
            ("Use variable Foo for this operation", "code", "code"),
            ("Use variable foo for this operation", "code", "code"),
            ("Use the square expression here", "x²", "math"),
            ("Use the square expression here", "x2", "math"),
            ("Python indentation changes blocks", "```\nif a:\n    b()\n    c()\n```", "code"),
            ("Python indentation changes blocks", "```\nif a:\n    b()\nc()\n```", "code"),
        ]
        for claim, detail, topic in variants:
            self.add(claim, detail, topic=topic)
        self.assertEqual(cleanup.clean()["removed"], 0)
        self.assertEqual(memory.count_points(), len(variants))

    def test_paragraph_whitespace_duplicates_clean_once_and_second_click_is_noop(self):
        self.add(detail="Resolution controls\n the density.")
        self.add(detail="Resolution controls the density.")
        self.assertEqual(cleanup.clean()["removed"], 1)
        self.assertEqual(cleanup.clean()["removed"], 0)
        self.assertTrue(cleanup.state()["available"])
        self.assertEqual(memory.conn().execute("SELECT count(*) FROM cleanup_runs").fetchone()[0], 1)

    def test_failure_rolls_back_archive_and_deletions_together(self):
        self.add()
        self.add()
        memory.conn().execute("CREATE TRIGGER reject_delete BEFORE DELETE ON knowledge BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):
            cleanup.clean()
        self.assertEqual(memory.count_points(), 2)
        self.assertEqual(memory.conn().execute("SELECT count(*) FROM cleanup_runs").fetchone()[0], 0)
        self.assertEqual(memory.conn().execute("SELECT count(*) FROM cleanup_archive").fetchone()[0], 0)

    def test_cache_invalidates_and_empty_undo_is_safe(self):
        self.add()
        self.add()
        self.assertEqual(len(memory.search("query", vec=[1., 0.])), 2)
        version = memory.version()
        cleanup.clean()
        self.assertGreater(memory.version(), version)
        self.assertEqual(len(memory.search("query", vec=[1., 0.])), 1)
        cleanup.undo()
        self.assertEqual(len(memory.search("query", vec=[1., 0.])), 2)
        self.assertFalse(cleanup.undo()["ok"])

    def test_busy_learning_prevents_cleanup(self):
        self.add()
        self.add()
        with patch.dict(evolve.engine.state, running=True):
            self.assertFalse(cleanup.clean()["ok"])
        self.assertEqual(memory.count_points(), 2)

    def test_different_embedding_versions_preserve_search_coverage(self):
        pid = self.add()
        point = memory.get_point(pid)
        memory.add_point(topic=point["topic"], claim=point["claim"], detail=point["detail"], vec=[1., 0., 0.], dedup=False)
        self.assertEqual(cleanup.clean()["removed"], 0)
        self.assertEqual(len(memory.search("q", vec=[1., 0.])), 1)
        self.assertEqual(len(memory.search("q", vec=[1., 0., 0.])), 1)

    def test_static_routes_and_archived_point_detail_remain_available(self):
        from backend.main import app
        first = self.add()
        self.add(confidence=.9)
        client = TestClient(app)
        try:
            self.assertEqual(client.get("/api/knowledge/cleanup").status_code, 200)
            self.assertEqual(client.post("/api/knowledge/cleanup").json()["removed"], 1)
            self.assertTrue(client.get("/api/knowledge/" + first).json()["item"]["archived"])
            self.assertEqual(client.post("/api/knowledge/cleanup/undo").json()["restored"], 1)
        finally:
            client.close()
