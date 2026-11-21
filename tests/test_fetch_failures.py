"""空转与重复抓取：读取失败台账、读不出的站点、检索限流冷却、残留会话复位。

真实库里的症状（2026-11）：4 个动态渲染首页各被重读约 1250 次；
「所有检索源都返回空」6977 次而持续自学仍每 1-30 秒开一次；16 个会话卡在「进行中」。
"""
from __future__ import annotations

import time
from unittest.mock import patch

from test_regressions import IsolatedTest
from backend import config, evolve, memory, scheduler

SHORT = "正文过短（可能是动态渲染页面）"


def page(url):
    return {"title": url, "url": url, "snippet": "", "kind": "web"}


class FailureLedgerTests(IsolatedTest):
    def test_hard_failure_twice_blocks_url_and_success_clears_it(self):
        url = "https://help.example.com/ag/en-us"
        memory.record_fetch(url, False, SHORT)
        self.assertEqual(memory.fetch_blocked([url]), {})  # 一次可能是偶发
        memory.record_fetch(url, False, SHORT)
        self.assertIn(url, memory.fetch_blocked([url]))
        memory.record_fetch(url, True)
        self.assertEqual(memory.fetch_blocked([url]), {})

    def test_http_4xx_is_hard_but_429_and_timeouts_are_soft(self):
        a, b = "https://x.example/404", "https://x.example/slow"
        for _ in range(2):
            memory.record_fetch(a, False, "HTTP 404")
            memory.record_fetch(b, False, "HTTP 429")
        blocked = memory.fetch_blocked([a, b])
        self.assertIn(a, blocked)
        self.assertNotIn(b, blocked)  # 软失败要 3 次
        memory.record_fetch(b, False, "ReadTimeout")
        self.assertIn(b, memory.fetch_blocked([b]))

    def test_soft_failures_expire_after_one_day(self):
        url = "https://x.example/flaky"
        for _ in range(3):
            memory.record_fetch(url, False, "ConnectError")
        later = time.time() + 1.5 * 86400
        with patch.object(memory.time, "time", return_value=later):
            self.assertEqual(memory.fetch_blocked([url]), {})

    def test_hard_failures_expire_after_retry_window(self):
        url = "https://x.example/spa"
        memory.record_fetch(url, False, SHORT)
        memory.record_fetch(url, False, SHORT)
        later = time.time() + (config.get("doc_retry_days", 7) + 1) * 86400
        with patch.object(memory.time, "time", return_value=later):
            self.assertEqual(memory.fetch_blocked([url]), {})

    def test_stop_is_not_a_failure(self):
        url = "https://x.example/stopped"
        for _ in range(5):
            memory.record_fetch(url, False, "已停止")
        self.assertEqual(memory.fetch_blocked([url]), {})

    def test_never_readable_host_is_blocked_but_mostly_good_host_is_not(self):
        for i in range(memory.HOST_HARD_LIMIT):
            memory.record_fetch(f"https://spa.example/t/{i}", False, SHORT)
        # maxon 的真实情况：528 篇能读、少数首页读不出 —— 不能整站封
        memory.record_fetch("https://docs.example/ok", True)
        for i in range(memory.HOST_HARD_LIMIT + 2):
            memory.record_fetch(f"https://docs.example/index{i}", False, SHORT)
        fresh_spa, fresh_doc = "https://spa.example/t/new", "https://docs.example/new"
        blocked = memory.fetch_blocked([fresh_spa, fresh_doc])
        self.assertEqual(blocked.get(fresh_spa), "站点读不出正文")
        self.assertNotIn(fresh_doc, blocked)
        self.assertEqual(memory.blocked_hosts(), {"spa.example"})
        self.assertIn("spa.example", evolve._avoid_hint())

    def test_backfill_from_documents_history(self):
        c = memory.conn()
        now = time.time()
        with c:
            c.execute("DELETE FROM maintenance WHERE name='fetch_failures_backfill'")
            c.executemany("INSERT OR REPLACE INTO documents(url,title,topic,chars,ok,fetched_at)"
                          " VALUES (?,?,?,?,?,?)", [
                              ("https://old.example/recent-fail", "", "t", 0, 0, now - 3600),
                              ("https://old.example/ancient-fail", "", "t", 0, 0, now - 400 * 86400),
                              ("https://old.example/good", "", "t", 900, 1, now - 3600),
                          ])
        memory._backfill_fetch_failures(c)
        blocked = memory.fetch_blocked(["https://old.example/recent-fail", "https://old.example/ancient-fail"])
        self.assertEqual(set(blocked), {"https://old.example/recent-fail"})
        self.assertEqual(memory.blocked_hosts(), set())  # 该站有成功记录
        # 只补一次
        memory._backfill_fetch_failures(c)
        self.assertEqual(c.execute("SELECT fails FROM fetch_failures WHERE url=?",
                                   ("https://old.example/recent-fail",)).fetchone()[0], 2)


class EngineSkipTests(IsolatedTest):
    def test_pick_targets_skips_repeatedly_failing_urls(self):
        engine = evolve.Engine()
        engine.state["direction"] = "ZBrush"
        bad, good = page("https://help.example.com/ag/en-us"), page("https://help.example.com/ok")
        memory.record_fetch(bad["url"], False, SHORT)
        memory.record_fetch(bad["url"], False, SHORT)
        self.assertEqual([r["url"] for r in engine._pick_targets([bad, good])], [good["url"]])

    def test_fetch_records_failures_in_ledger(self):
        engine = evolve.Engine()
        engine.state["direction"] = "ZBrush"
        url = "https://help.example.com/ag/en-us"
        doc = {"ok": False, "url": url, "title": "", "text": "", "error": SHORT}
        with patch.object(evolve.fetcher, "fetch", return_value=doc):
            engine._fetch_all([page(url)])
            engine._fetch_all([page(url)])
        self.assertIn(url, memory.fetch_blocked([url]))

    def test_ledger_failure_never_breaks_learning(self):
        engine = evolve.Engine()
        with patch.object(memory, "fetch_blocked", side_effect=RuntimeError("db locked")):
            self.assertEqual(engine._blocked(["https://x.example/a"]), {})


class SearchBackoffTests(IsolatedTest):
    def test_backoff_doubles_caps_and_resets_on_success(self):
        waits = [evolve._note_search(True) for _ in range(8)]
        self.assertEqual(waits[:3], [60.0, 120.0, 240.0])
        self.assertEqual(waits[-1], evolve.SEARCH_BACKOFF_MAX)
        self.assertGreater(evolve.search_cooldown(), 0)
        evolve._note_search(False)
        self.assertEqual(evolve.search_cooldown(), 0)

    def test_empty_search_starts_cooldown(self):
        engine = evolve.Engine()
        with patch.object(evolve.search, "search", return_value=[]):
            engine._search_all(["q1", "q2"])
        self.assertGreater(evolve.search_cooldown(), 0)

    def test_scheduler_waits_out_cooldown_instead_of_starting(self):
        config.update({"last_direction": "ZBrush"})
        evolve._note_search(True)
        with patch.dict(scheduler._state, enabled=True, last_run=0., next_run=0.), \
                patch.object(evolve.engine, "start", return_value={"ok": True}) as start:
            scheduler._tick()
            start.assert_not_called()
            self.assertIn("限流", scheduler._state["waiting"])
            self.assertGreater(scheduler._state["next_run"], time.time())

    def test_run_stops_after_first_round_while_cooling_down(self):
        engine = evolve.Engine()
        rounds = []

        def fake_round(direction, r, total, sid):
            rounds.append(r)
            evolve._note_search(True)  # 第 1 轮检索全空

        with patch.object(evolve.llm, "tags", return_value=(True, [])), \
                patch.object(evolve.llm, "ensure_models"), \
                patch.object(engine, "_round", side_effect=fake_round):
            sid = memory.start_session("ZBrush")
            engine._run("ZBrush", 5, sid)
        self.assertEqual(rounds, [1])


class StaleSessionTests(IsolatedTest):
    def test_running_sessions_closed_on_startup(self):
        sid = memory.start_session("ZBrush")
        self.assertEqual(memory.close_stale_sessions(), 1)
        row = memory.conn().execute("SELECT status, ended_at FROM sessions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row[0], "stopped")
        self.assertIsNotNone(row[1])
        self.assertEqual(memory.close_stale_sessions(), 0)
