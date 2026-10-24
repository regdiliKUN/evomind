"""One-click book refinement: full coverage, durable resumption and cancellation."""
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_regressions import IsolatedTest
from backend import memory, refinement


class BookRefinementTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.state_patch = patch.dict(refinement._state, running=False, stage="idle", done=0,
                                      total=0, error="", saved=0, skipped=0, failed=0,
                                      mode="books", batch=0, batches=0, current="")
        self.stop_patch = patch.object(refinement, "_stop", threading.Event())
        self.state_patch.start()
        self.stop_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.stop_patch.stop)

    def book_point(self, i, book="book-a", topic="shared", kind="book"):
        return memory.add_point(topic=topic, claim=f"Knowledge point {i}: detailed original learning content",
                                detail="Necessary original conditions and supporting explanation. " * 4,
                                source_url=f"file:///{book}.txt", source_title=f"《{book}》 chapter",
                                source_kind=kind, book_id=book, vec=[1., .1], dedup=False)

    @staticmethod
    def distill(points, evidence):
        first = points[0]
        ids = first.get("source_ids") or [first["id"]]
        return [{"claim": "Concise core knowledge", "detail": "", "source_ids": ids}]

    def run_pending(self):
        batches, total, skipped = refinement._book_batches()
        refinement._stop.clear()
        refinement._state.update(running=True, stage="refining", total=total, done=skipped,
                                 skipped=skipped, failed=0, saved=0, batches=len(batches), error="")
        refinement._run_books(batches)
        return refinement.state()

    def test_all_books_exceed_120_and_do_not_mix_books_or_web(self):
        for i in range(131):
            self.book_point(i)
        for i in range(3):
            self.book_point(200 + i, book="book-b")
        self.book_point(300, book="", topic="legacy book")
        web = self.book_point(400, book="", kind="web")
        batches, total, skipped = refinement._book_batches()
        self.assertEqual((total, skipped), (135, 0))
        self.assertTrue(all(len(job["points"]) <= 60 for job in batches))
        self.assertNotIn(web, {p["id"] for b in batches for p in b["points"]})
        self.assertTrue(all(len({p["book_id"] for p in b["points"]}) == 1 for b in batches))
        with patch.object(refinement, "_distill", side_effect=self.distill):
            state = self.run_pending()
        self.assertEqual(state["stage"], "done")
        self.assertEqual(state["done"], 135)
        self.assertEqual(memory.count_points(), 136)
        self.assertEqual(memory.conn().execute("SELECT count(*) FROM refinement_coverage").fetchone()[0], 135)
        self.assertEqual(refinement._book_batches()[1:], (135, 135))

    def test_unchanged_inputs_skip_and_only_changed_or_new_points_resume(self):
        ids = [self.book_point(i) for i in range(4)]
        with patch.object(refinement, "_distill", side_effect=self.distill):
            self.run_pending()
        with patch.object(refinement.threading, "Thread") as thread:
            result = refinement.start_books()
            self.assertTrue(result["completed"])
            thread.assert_not_called()
        c = memory.conn()
        c.execute("UPDATE knowledge SET detail='Edited knowledge source detail' WHERE id=?", (ids[0],))
        c.commit()
        new = self.book_point(9)
        batches, total, skipped = refinement._book_batches()
        self.assertEqual((total, skipped), (5, 3))
        self.assertEqual({p["id"] for b in batches for p in b["points"]}, {ids[0], new})

    def test_cancellation_retains_completed_batches_for_resumption(self):
        for i in range(5):
            self.book_point(i)
        calls = 0
        def stop_second(points, evidence):
            nonlocal calls
            calls += 1
            if calls == 2:
                refinement.stop()
                raise refinement.RefinementStopped()
            return self.distill(points, evidence)
        with patch.object(refinement, "BOOK_BATCH_SIZE", 2), \
             patch.object(refinement, "_distill", side_effect=stop_second):
            state = self.run_pending()
        self.assertEqual((state["stage"], state["done"], state["saved"]), ("stopped", 2, 1))
        self.assertFalse(state["running"])
        with patch.object(refinement, "BOOK_BATCH_SIZE", 2), \
             patch.object(refinement, "_distill", side_effect=self.distill):
            state = self.run_pending()
        self.assertEqual((state["stage"], state["skipped"], state["done"]), ("done", 2, 5))

    def test_failed_batches_are_not_marked_complete_and_can_be_retried(self):
        for i in range(6):
            self.book_point(i)
        calls = 0
        def fail_first(points, evidence):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("model failed")
            return self.distill(points, evidence)
        with patch.object(refinement, "BOOK_BATCH_SIZE", 2), \
             patch.object(refinement, "_distill", side_effect=fail_first):
            state = self.run_pending()
        self.assertEqual((state["stage"], state["failed"], state["saved"]), ("partial", 1, 2))
        self.assertEqual(refinement._book_batches()[1:], (6, 4))
        with patch.object(refinement, "_distill", side_effect=self.distill):
            self.assertEqual(self.run_pending()["stage"], "done")
        self.assertEqual(refinement._book_batches()[1:], (6, 6))

    def test_repeated_failures_stop_instead_of_hammering_the_model(self):
        for i in range(8):
            self.book_point(i)
        with patch.object(refinement, "BOOK_BATCH_SIZE", 2), \
             patch.object(refinement, "_distill", side_effect=ValueError("offline")) as distill:
            state = self.run_pending()
        self.assertEqual(distill.call_count, 3)
        self.assertEqual(state["stage"], "error")
        self.assertEqual(refinement._book_batches()[2], 0)

    def test_stop_between_generation_and_verification_does_not_launch_another_call(self):
        def generated(*args, **kwargs):
            refinement.stop()
            return {"items": []}
        with patch.object(refinement.llm, "chat_json", side_effect=generated) as chat:
            with self.assertRaises(refinement.RefinementStopped):
                refinement._distill([], {})
        self.assertEqual(chat.call_count, 1)

    def test_coverage_and_result_commit_are_atomic(self):
        pid = self.book_point(1)
        points = [dict(memory.conn().execute("SELECT * FROM knowledge WHERE id=?", (pid,)).fetchone())]
        memory.conn().execute("CREATE TRIGGER reject_coverage BEFORE INSERT ON refinement_coverage BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(Exception):
            refinement._save("shared", points, self.distill([{"id": pid}], {}), "book-a", "Book A")
        self.assertEqual(refinement.history_count(), 0)
        self.assertEqual(memory.conn().execute("SELECT count(*) FROM refinement_coverage").fetchone()[0], 0)

    def test_history_paginates_all_saved_batches_with_book_names(self):
        pid = self.book_point(1)
        rows = [dict(memory.conn().execute("SELECT * FROM knowledge WHERE id=?", (pid,)).fetchone())]
        for _ in range(25):
            refinement._save("shared", rows, self.distill([{"id": pid}], {}), "book-a", "Book A")
        first, second = refinement.history(limit=20), refinement.history(limit=20, offset=20)
        self.assertEqual((len(first), len(second), refinement.history_count()), (20, 5, 25))
        self.assertFalse({r["id"] for r in first} & {r["id"] for r in second})
        self.assertEqual(first[0]["book_title"], "Book A")

    def test_empty_books_and_thread_spawn_failure_are_safe(self):
        self.assertFalse(refinement.start_books()["ok"])
        self.book_point(1)
        with patch.object(refinement.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("spawn failed")
            with self.assertRaises(RuntimeError):
                refinement.start_books()
        self.assertFalse(refinement.state()["running"])

    def test_one_click_api_has_no_topic_parameter_and_validates_pagination(self):
        from backend.main import app
        client = TestClient(app)
        try:
            with patch.object(refinement, "start_books", return_value={"ok": True, "total": 200}) as start:
                self.assertTrue(client.post("/api/refinements/books/start").json()["ok"])
                start.assert_called_once_with()
            self.assertEqual(client.get("/api/refinements?limit=0").status_code, 422)
            self.assertEqual(client.get("/api/refinements?offset=-1").status_code, 422)
        finally:
            client.close()
