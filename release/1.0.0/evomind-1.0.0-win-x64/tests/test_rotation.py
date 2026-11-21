"""方向轮换（学习方向自动更换）的回归测试。

全部离线：模型调用与学习引擎都被替换成确定性桩，不写用户知识库。
"""
import json
import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import config, curriculum, evolve, memory
from test_regressions import IsolatedTest

SEED = "ZBrush"


class RotationTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.rot = patch.dict(curriculum._state, {
            "active": False, "running": False, "paused": False, "in_batch": False,
            "stage": "idle", "seed": "", "run_id": "", "plan": [], "current": "",
            "current_id": "", "rounds_in_current": 0, "rounds_per_direction": 100,
            "total_round_limit": 300, "total_rounds_done": 0, "subdirection_count": 6,
            "switches": [], "summary": None, "error": "", "stop_reason": "",
            "started_at": 0.0, "finished_at": 0.0, "failures": 0, "blocked": "",
            "skip_requested": False, "retry_in": 0, "preset": [],
            "thread": None,
        })
        self.rot.start()
        # _add_plan 是原地 extend 的，patch.dict 恢复的是同一个列表对象，
        # 不清空会把上一个用例的方向带进来
        curriculum._state["plan"] = []
        curriculum._state["switches"] = []
        curriculum._stop_evt.clear()
        curriculum._scope_cache.clear()

    def tearDown(self):
        curriculum._stop_evt.set()
        curriculum._wake_evt.set()
        curriculum._state["plan"] = []
        curriculum._state["switches"] = []
        self.rot.stop()
        super().tearDown()

    # ---------- 拆解 ----------
    def test_split_dedupes_numbering_and_mother_direction(self):
        payload = {"items": ["1. DynaMesh 面数与细节取舍", "DynaMesh 面数与细节取舍",
                             "ZBrush", "细分层级分配", "  ", "笔刷强度与焦点设置"]}
        with patch.object(curriculum.llm, "chat_json", return_value=payload):
            items, source = curriculum._split(SEED, 3)
        self.assertEqual(items[:2], ["DynaMesh 面数与细节取舍", "细分层级分配"])
        self.assertNotIn("ZBrush", items)
        self.assertEqual(len(items), len({i.casefold() for i in items}))
        self.assertEqual(source, "model")

    def test_split_falls_back_to_local_deck_when_model_fails(self):
        with patch.object(curriculum.llm, "chat_json", side_effect=RuntimeError("offline")):
            items, source = curriculum._split(SEED, 4)
        self.assertEqual(len(items), 4)
        self.assertEqual(source, "local")
        # 本地池按母方向拼装，天然在范围内
        self.assertTrue(all(SEED in i for i in items))

    def test_split_tops_up_when_model_returns_too_few(self):
        with patch.object(curriculum.llm, "chat_json", return_value={"items": ["DynaMesh 面数取舍"]}):
            items, source = curriculum._split(SEED, 5)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0], "DynaMesh 面数取舍")
        self.assertEqual(source, "model+local")

    # ---------- 边界 ----------
    def test_local_composition_passes_without_calling_the_model(self):
        with patch.object(curriculum.llm, "chat_json", side_effect=AssertionError("不该调用模型")):
            ok, why = curriculum.in_scope("光固化 3D 打印", "光固化 3D 打印：收缩补偿模型")
        self.assertTrue(ok)
        self.assertIn("关键词", why)

    def test_neighbor_domain_is_rejected_by_the_judge(self):
        with patch.object(curriculum.llm, "chat_json",
                          return_value={"in_scope": False, "reason": "属于 Blender 领域"}) as judge:
            ok, why = curriculum.in_scope(SEED, "Blender 拓扑重网格流程")
        self.assertFalse(ok)
        self.assertIn("Blender", why)
        judge.assert_called_once()

    def test_judge_failure_does_not_kill_the_rotation(self):
        with patch.object(curriculum.llm, "chat_json", side_effect=RuntimeError("timeout")):
            ok, why = curriculum.in_scope(SEED, "DynaMesh 面数与细节取舍")
        self.assertTrue(ok)
        self.assertIn("审核不可用", why)

    # ---------- 选向 ----------
    def test_out_of_scope_candidate_is_dropped_then_reselected(self):
        bad = curriculum._add_plan(["Blender 拓扑重网格流程"], "model")[0]
        good = curriculum._add_plan(["DynaMesh 面数与细节取舍"], "model")[0]
        verdicts = {"Blender 拓扑重网格流程": (False, "相邻软件"), "DynaMesh 面数与细节取舍": (True, "")}
        order = [bad, good]
        with patch.object(curriculum, "in_scope", side_effect=lambda s, c: verdicts[c]), \
             patch.object(curriculum, "_next_candidate",
                          side_effect=lambda exclude, allow_replenish=True: order.pop(0) if order else None):
            picked = curriculum._choose(SEED)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], good["id"])
        self.assertEqual(bad["status"], "rejected")

    def test_only_out_of_scope_candidates_fall_back_to_a_legal_local_direction(self):
        curriculum._add_plan(["Blender 拓扑重网格流程"], "model")
        with patch.object(curriculum, "in_scope", return_value=(False, "相邻软件")), \
             patch.object(curriculum, "_replenish", return_value=False):
            picked = curriculum._choose(SEED)
        self.assertIsNotNone(picked)
        self.assertIn(SEED, picked["direction"])

    def test_recent_window_is_avoided(self):
        entries = curriculum._add_plan([f"{SEED}：主题{i}" for i in range(4)], "model")
        curriculum._state.update(seed=SEED, current=entries[0]["direction"], current_id=entries[0]["id"])
        curriculum._state["switches"] = [
            {"to": entries[1]["direction"]}, {"to": entries[2]["direction"]},
        ]
        picks = {curriculum._choose(SEED)["direction"] for _ in range(12)}
        self.assertNotIn(entries[0]["direction"], picks)
        self.assertNotIn(entries[1]["direction"], picks)
        self.assertNotIn(entries[2]["direction"], picks)

    def test_pool_is_replenished_when_all_directions_are_used(self):
        curriculum._state.update(seed=SEED, subdirection_count=6)
        entries = curriculum._add_plan([f"{SEED}：主题{i}" for i in range(2)], "model")
        for e in entries:
            e["status"] = "done"
        with patch.object(curriculum, "_split", return_value=([f"{SEED}：补充主题"], "model")):
            picked = curriculum._next_candidate(set())
        self.assertEqual(picked["direction"], f"{SEED}：补充主题")
        self.assertEqual(len(curriculum._state["plan"]), 3)

    # ---------- 轮次推进 ----------
    def _prime(self, quota=2, limit=10, count=3):
        rid = memory.curriculum_start_run(SEED, quota, limit, count)
        curriculum._state.update(
            active=True, seed=SEED, run_id=rid, rounds_per_direction=quota,
            total_round_limit=limit, subdirection_count=count, started_at=time.time(),
        )
        entries = curriculum._add_plan([f"{SEED}：主题{i}" for i in range(count)], "model")
        return rid, entries

    def test_switches_after_the_configured_rounds(self):
        _, entries = self._prime(quota=2)
        calls = []

        def fake_batch(direction, rounds, run_id):
            calls.append((direction, rounds))
            return rounds, 1, ""

        with patch.object(curriculum, "_run_batch", side_effect=fake_batch):
            curriculum._iteration()   # 首个子方向 + 2 轮
            curriculum._iteration()   # 满 2 轮 → 换向 + 2 轮
            curriculum._iteration()   # 再换一次

        self.assertEqual([c[1] for c in calls], [2, 2, 2])
        self.assertEqual(len({c[0] for c in calls}), 3)
        self.assertEqual(curriculum._state["total_rounds_done"], 6)
        self.assertEqual(len(curriculum._state["switches"]), 3)

    def test_batch_is_truncated_by_the_total_limit(self):
        self._prime(quota=10, limit=3)
        calls = []
        with patch.object(curriculum, "_run_batch",
                          side_effect=lambda d, r, run: (calls.append((d, r)) or (r, 0, ""))):
            curriculum._iteration()
            curriculum._iteration()
        self.assertEqual(calls[0][1], 3)
        self.assertFalse(curriculum._state["active"])
        self.assertEqual(curriculum._state["stop_reason"], "total_rounds")

    def test_long_quota_is_split_into_engine_sized_batches(self):
        self._prime(quota=250, limit=300)
        calls = []
        with patch.object(curriculum, "_run_batch",
                          side_effect=lambda d, r, run: (calls.append(r) or (r, 0, ""))):
            curriculum._iteration()
            curriculum._iteration()
        self.assertEqual(calls, [curriculum.BATCH_MAX, curriculum.BATCH_MAX])

    def test_empty_batch_backs_off_after_the_threshold(self):
        self._prime(quota=5)
        with patch.object(curriculum, "_run_batch", return_value=(0, 0, "Ollama 未运行")), \
             patch.object(curriculum._wake_evt, "wait", return_value=True), \
             patch.object(curriculum.time, "sleep"):
            curriculum._iteration()
            self.assertFalse(curriculum._state["paused"])
            self.assertEqual(curriculum._state["failures"], 1)
            curriculum._iteration()
        # 阈值（默认 2）之后进入退避重试，而不是直接暂停
        self.assertFalse(curriculum._state["paused"])
        self.assertEqual(curriculum._state["stage"], "waiting_model")

    # ---------- 暂停 / 恢复 ----------
    def test_pause_keeps_remaining_rounds_and_resume_continues(self):
        self._prime(quota=4)
        with patch.object(curriculum, "_run_batch", return_value=(1, 1, "")):
            curriculum._iteration()
        self.assertEqual(curriculum._state["rounds_in_current"], 1)

        with patch.object(curriculum.evolve.engine, "stop") as stop:
            curriculum.pause()
        stop.assert_called_once()
        self.assertTrue(curriculum._state["paused"])

        with patch.object(curriculum, "_run_batch", return_value=(0, 0, "")) as run:
            curriculum._iteration()          # 暂停期间不推进
        run.assert_not_called()

        curriculum.resume()
        self.assertFalse(curriculum._state["paused"])
        with patch.object(curriculum, "_run_batch", return_value=(3, 2, "")) as run:
            curriculum._iteration()
        self.assertEqual(run.call_args[0][1], 3)   # 只剩 3 轮
        self.assertEqual(curriculum._state["total_rounds_done"], 4)

    # ---------- 汇总 ----------
    def test_summary_lists_each_direction_with_results(self):
        rid, entries = self._prime(quota=2)
        with patch.object(curriculum, "_run_batch", return_value=(2, 1, "")):
            curriculum._iteration()
            first = curriculum._state["current"]
            memory.add_point(topic=first, claim="DynaMesh 面数过高会拖慢雕刻响应",
                             detail="测试用结论", vec=[1.0, 0.0], dedup=False)
            curriculum._iteration()
        curriculum.stop()

        summ = curriculum.summary()
        self.assertEqual(summ["seed"], SEED)
        self.assertEqual(summ["total_rounds"], 4)
        self.assertEqual(summ["points_total"], 2)
        rows = {i["direction"]: i for i in summ["directions"]}
        self.assertEqual(rows[first]["rounds"], 2)
        self.assertTrue(rows[first]["highlights"])
        self.assertEqual(len(rows), 2)   # 没学过的方向不进汇总
        # 收尾后不能还挂着「进行中」；首次选向不算切换次数
        self.assertTrue(all(i["status"] != "active" for i in summ["directions"]))
        self.assertEqual(summ["switch_count"], 1)

        run = memory.curriculum_get_run(rid)
        self.assertEqual(run["status"], "stopped")
        self.assertEqual(run["total_rounds"], 4)
        self.assertIn("方向轮换汇总", run["summary"])

    def test_stop_finishes_immediately_when_no_batch_is_running(self):
        self._prime(quota=5)
        curriculum._state.update(current="ZBrush：主题0", stage="learning")
        with patch.object(curriculum.evolve.engine, "stop"):
            res = curriculum.stop()
        self.assertFalse(curriculum._state["active"])
        self.assertEqual(res["summary"]["total_rounds"], 0)

    # ---------- API ----------
    def test_api_validates_and_runs_the_full_lifecycle(self):
        from backend.main import app

        client = TestClient(app)
        self.assertEqual(client.post("/api/rotation/start", json={"seed": "  "}).status_code, 400)
        self.assertEqual(
            client.post("/api/rotation/start",
                        json={"seed": SEED, "subdirection_count": 1}).status_code, 422
        )
        bad = client.post("/api/rotation/start", json={"seed": SEED, "total_rounds": 0})
        self.assertEqual(bad.status_code, 422)

        with patch.object(curriculum, "_loop", return_value=None):
            res = client.post("/api/rotation/start",
                              json={"seed": SEED, "rounds_per_direction": 100,
                                    "total_rounds": 300, "subdirection_count": 6})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(curriculum.state()["active"])
        self.assertEqual(curriculum.state()["rounds_per_direction"], 100)
        self.assertEqual(config.get("rotation_total_rounds"), 300)

        # 重复启动被拒
        again = client.post("/api/rotation/start", json={"seed": SEED}).json()
        self.assertFalse(again["ok"])

        self.assertTrue(client.post("/api/rotation/pause").json()["ok"])
        self.assertTrue(curriculum.state()["paused"])
        self.assertTrue(client.post("/api/rotation/resume").json()["ok"])
        self.assertFalse(curriculum.state()["paused"])

        stopped = client.post("/api/rotation/stop").json()
        self.assertTrue(stopped["ok"])
        self.assertFalse(curriculum.state()["active"])
        self.assertEqual(client.get("/api/rotation").status_code, 200)

    def test_status_exposes_rotation_without_touching_the_database(self):
        from backend.main import api_status

        with patch.object(curriculum, "_last_run_view",
                          side_effect=AssertionError("状态轮询不该查库")):
            out = api_status()
        self.assertIn("rotation", out)
        self.assertIn("direction_rotation", out["capabilities"])

    def test_preview_rejects_invalid_count(self):
        with self.assertRaises(ValueError):
            curriculum.preview("", 6)
        with self.assertRaises(ValueError):
            curriculum.preview(SEED, 1)
        with patch.object(curriculum.llm, "chat_json",
                          return_value={"items": ["DynaMesh 面数与细节取舍"]}):
            out = curriculum.preview(SEED, 3)
        self.assertEqual(out["seed"], SEED)
        self.assertTrue(out["items"])

    def test_plan_is_persisted_and_reloadable_after_restart(self):
        rid, entries = self._prime(quota=2)
        rows = memory.curriculum_list_directions(rid)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(memory.curriculum_last_run()["id"], rid)
        self.assertEqual(len(memory.curriculum_runs()), 1)
        # 内存态清空后仍能读出上一次任务
        with patch.dict(curriculum._state, {"plan": [], "run_id": "", "summary": None}):
            last = curriculum._last_run_view()
        self.assertEqual(last["run_id"], rid)
        self.assertEqual(len(last["directions"]), 0)   # 还没学过的方向不展示

    # ---------- 生命周期竞态 ----------
    def test_restart_is_refused_while_the_previous_thread_is_finishing(self):
        class Alive:
            def is_alive(self):
                return True

        curriculum._state.update(active=False, thread=Alive())
        res = curriculum.start(SEED, rounds_per_direction=1, total_rounds=1, subdirection_count=2)
        self.assertFalse(res["ok"])
        self.assertIn("收尾", res["msg"])

    def test_batch_result_is_dropped_when_the_run_was_replaced(self):
        self._prime(quota=5)

        def take_over(direction, rounds, run_id):
            curriculum._state["run_id"] = "another-run"
            return rounds, 9, ""

        with patch.object(curriculum, "_run_batch", side_effect=take_over):
            curriculum._iteration()
        self.assertEqual(curriculum._state["total_rounds_done"], 0)
        self.assertEqual(curriculum._state["rounds_in_current"], 0)

    def test_direction_switch_does_not_tear_down_http_clients(self):
        from backend import httpc

        with patch.object(httpc, "reset") as reset:
            config.update({"last_direction": "ZBrush：主题0"})
        reset.assert_not_called()
        with patch.object(httpc, "reset") as reset:
            config.update({"proxy": "http://127.0.0.1:7890"})
        reset.assert_called_once()

    def test_engine_start_exception_becomes_a_retryable_failure(self):
        self._prime(quota=2)
        with patch.object(curriculum.evolve.engine, "start", side_effect=RuntimeError("boom")), \
             patch.object(curriculum.time, "sleep"):
            curriculum._iteration()
        self.assertEqual(curriculum._state["total_rounds_done"], 0)
        self.assertEqual(curriculum._state["failures"], 1)
        self.assertTrue(curriculum._state["active"])

    # ---------- 批次等待 ----------
    def test_batch_waits_on_the_idle_signal_not_the_state_flag(self):
        with patch.object(curriculum.evolve.engine, "start", return_value={"ok": True}), \
             patch.object(curriculum.evolve.engine, "wait_idle", return_value=True) as wait, \
             patch.dict(curriculum.evolve.engine.state, running=True, completed=2, points=5):
            done, points, err = curriculum._run_batch("ZBrush：主题0", 2, curriculum._state["run_id"])
        wait.assert_called()
        self.assertEqual((done, points, err), (2, 5, ""))

    def test_engine_signals_idle_and_reports_completed_rounds(self):
        engine = evolve.Engine()
        self.assertTrue(engine.wait_idle(0))
        with patch.object(evolve.llm, "tags", return_value=(True, [])), \
             patch.object(evolve.llm, "ensure_models", return_value=None), \
             patch.object(engine, "_round", return_value=None):
            res = engine.start("ZBrush：主题0", 2)
            self.assertTrue(res["ok"])
            self.assertTrue(engine.wait_idle(10))
        self.assertEqual(engine.state["completed"], 2)
        self.assertFalse(engine.state["running"])

    def test_switch_message_reports_the_truncated_budget(self):
        self._prime(quota=5, limit=3)
        seen = []

        def listen(evt):
            seen.append(evt)

        evolve.bus.subscribe(listen)
        try:
            with patch.object(curriculum, "in_scope", return_value=(True, "")):
                curriculum._switch("开始首个子方向")
        finally:
            evolve.bus.unsubscribe(listen)
        msg = [e["msg"] for e in seen if e["level"] == "ok"][-1]
        self.assertIn("计划再学 3 轮", msg)
        self.assertIn("截断", msg)

    def test_rejected_directions_do_not_consume_the_pool_quota(self):
        curriculum._state.update(seed=SEED, subdirection_count=2)   # 池上限 = 2 × 6
        entries = curriculum._add_plan([f"{SEED}：越界{i}" for i in range(12)], "model")
        for e in entries:
            e["status"] = "rejected"
        with patch.object(curriculum, "_split", return_value=([f"{SEED}：补充"], "model")):
            self.assertTrue(curriculum._replenish(set()))
        self.assertEqual(curriculum._state["plan"][-1]["direction"], f"{SEED}：补充")

    def test_replenish_is_attempted_at_most_once_per_switch(self):
        curriculum._state.update(seed=SEED, subdirection_count=2)
        calls = []

        def fake_split(seed, count, exclude=None):
            calls.append(count)
            return [f"{SEED}：新{i}" for i in range(count)], "model"

        with patch.object(curriculum, "_split", side_effect=fake_split), \
             patch.object(curriculum, "in_scope", return_value=(False, "越界")):
            picked = curriculum._choose(SEED)
        self.assertEqual(len(calls), 1, "一次换向只应补充一次子方向")
        self.assertIsNotNone(picked)

    def test_engine_busy_is_waited_out_without_counting_as_failure(self):
        self._prime(quota=2)
        busy = "已有学习任务在跑，请先停止"
        with patch.object(curriculum, "_run_batch", return_value=(0, 0, busy)), \
             patch.object(curriculum.time, "sleep"):
            for _ in range(3):
                curriculum._iteration()
        self.assertFalse(curriculum._state["paused"])
        self.assertEqual(curriculum._state["failures"], 0)
        self.assertTrue(curriculum._state["active"])
        self.assertEqual(curriculum._state["blocked"], busy)

    def test_each_direction_records_its_learning_sessions(self):
        self._prime(quota=2)
        seq = {"n": 0}

        def fake(direction, rounds, run_id):
            seq["n"] += 1
            evolve.engine.state["session_id"] = f"sess-{seq['n']}"
            return rounds, 3, ""

        with patch.object(curriculum, "_run_batch", side_effect=fake):
            curriculum._iteration()
            curriculum._iteration()
            curriculum._iteration()
        recorded = [(e["direction"], e["sessions"]) for e in curriculum._state["plan"] if e["rounds"]]
        self.assertEqual(len(recorded), 3)
        self.assertEqual(sorted(s[0]["id"] for _, s in recorded), ["sess-1", "sess-2", "sess-3"])
        self.assertTrue(all(s[0]["rounds"] == 2 for _, s in recorded))

        target = recorded[0][0]
        curriculum.stop()
        summ = curriculum.summary()
        row = next(i for i in summ["directions"] if i["direction"] == target)
        self.assertEqual(len(row["sessions"]), 1)
        # 落库后仍可读回
        db_row = next(d for d in memory.curriculum_list_directions(summ["run_id"])
                      if d["direction"] == target)
        self.assertIn(row["sessions"][0]["id"], db_row["sessions"])

    def test_state_exposes_status_text_and_switch_count(self):
        self._prime(quota=1, limit=3)
        with patch.object(curriculum, "_run_batch", return_value=(1, 1, "")):
            curriculum._iteration()
            curriculum._iteration()
        snap = curriculum.state()
        self.assertEqual(snap["status_text"], "进行中")
        self.assertEqual(snap["switch_count"], 1)
        curriculum.pause()
        self.assertEqual(curriculum.state()["status_text"], "已暂停")
        curriculum.stop()
        snap = curriculum.state()
        self.assertFalse(snap["active"])
        self.assertIn(snap["status_text"], ("已手动结束", "已完成"))

    def test_engine_and_rotation_share_one_event_sink(self):
        evolve.emit("ok", "统一出口测试")
        curriculum._emit("ok", "轮换事件测试")
        msgs = [e["msg"] for e in memory.recent_events(30)]
        self.assertIn("统一出口测试", msgs)
        self.assertIn("轮换事件测试", msgs)

    def test_start_warns_when_total_is_smaller_than_the_quota(self):
        seen = []

        def listen(evt):
            seen.append(evt)

        evolve.bus.subscribe(listen)
        try:
            with patch.object(curriculum, "_loop", return_value=None):
                res = curriculum.start(SEED, rounds_per_direction=100,
                                       total_rounds=10, subdirection_count=2)
        finally:
            evolve.bus.unsubscribe(listen)
        self.assertTrue(res["ok"])
        self.assertTrue(any(e["level"] == "warn" and "小于每方向轮数" in e["msg"] for e in seen))

    def test_directions_without_a_run_are_not_written_as_orphans(self):
        curriculum._state.update(run_id="")
        entries = curriculum._add_plan(["ZBrush：孤立方向"], "model")
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(curriculum._state["plan"]), 1)
        count = memory.conn().execute("SELECT COUNT(*) FROM curriculum_directions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_recent_claims_returns_latest_per_topic_in_one_pass(self):
        for i in range(5):
            memory.add_point(topic="A", claim=f"A 方向第 {i} 条结论内容", detail="", vec=[1.0, 0.0], dedup=False)
            memory.add_point(topic="B", claim=f"B 方向第 {i} 条结论内容", detail="", vec=[0.0, 1.0], dedup=False)
        got = memory.recent_claims(["A", "B", "空主题"], 3)
        self.assertEqual(len(got["A"]), 3)
        self.assertEqual(len(got["B"]), 3)
        self.assertEqual(got["A"][0], "A 方向第 4 条结论内容")   # created_at 倒序
        self.assertEqual(got["空主题"], [])

    def test_state_history_lists_recent_runs(self):
        rid, _ = self._prime(quota=1)
        snap = curriculum.state(include_last=True)
        self.assertEqual(snap["history"][0]["run_id"], rid)
        self.assertEqual(snap["history"][0]["status_text"], "进行中")
        # 普通状态轮询不查库，不返回 history
        self.assertNotIn("history", curriculum.state())

    def test_pause_while_a_batch_is_running_keeps_the_task_alive(self):
        self._prime(quota=5)
        curriculum._state.update(in_batch=True, current=f"{SEED}：主题0")
        with patch.object(curriculum.evolve.engine, "stop") as stop:
            curriculum.pause()
        stop.assert_called_once()
        self.assertTrue(curriculum._state["active"])
        self.assertEqual(curriculum._state["stage"], "pausing")

    def test_stopped_task_does_not_start_another_batch(self):
        self._prime(quota=5)
        curriculum._stop_evt.set()
        with patch.object(curriculum, "_run_batch") as run:
            curriculum._iteration()
        run.assert_not_called()

    def test_start_and_preview_share_the_same_validation(self):
        res = curriculum.start("", rounds_per_direction=1, total_rounds=1, subdirection_count=2)
        self.assertFalse(res["ok"])
        with self.assertRaises(ValueError) as ctx:
            curriculum.preview("", 6)
        self.assertEqual(res["msg"], str(ctx.exception))

    def test_switch_does_not_block_state_reads(self):
        self._prime(quota=1)
        real_switch = curriculum._switch
        seen = []

        def slow_switch(reason):
            # 模拟换向里的模型审核耗时；此时 state() 必须仍能被其他线程读到
            worker = threading.Thread(target=lambda: seen.append(curriculum.state()["stage"]))
            worker.start()
            worker.join(2)
            return real_switch(reason)

        with patch.object(curriculum, "_switch", side_effect=slow_switch), \
             patch.object(curriculum, "_run_batch", return_value=(1, 0, "")):
            curriculum._iteration()
        self.assertEqual(len(seen), 1, "换向期间状态读取被锁阻塞了")

    def test_summary_reports_elapsed_time(self):
        self._prime(quota=1, limit=2)
        with patch.object(curriculum, "_run_batch", return_value=(1, 2, "")):
            curriculum._iteration()
        curriculum.stop()
        summ = curriculum.summary()
        self.assertIn("elapsed_seconds", summ)
        self.assertGreaterEqual(summ["elapsed_seconds"], 0)
        self.assertIn("耗时", curriculum._summary_text(dict(summ, elapsed_seconds=125)))

    def test_partial_keyword_match_still_goes_to_the_judge(self):
        # 「FDM 与光固化打印…」只命中部分关键词，属于相邻工艺，不能走字面快路径
        with patch.object(curriculum.llm, "chat_json",
                          return_value={"in_scope": False, "reason": "FDM 是另一种工艺"}) as judge:
            ok, why = curriculum.in_scope("光固化 3D 打印", "FDM与光固化打印支撑结构生成逻辑差异")
        judge.assert_called_once()
        self.assertFalse(ok)
        self.assertIn("另一种工艺", why)

    def test_full_keyword_paraphrase_skips_the_judge(self):
        with patch.object(curriculum.llm, "chat_json",
                          side_effect=AssertionError("不该调用模型")):
            ok, _ = curriculum.in_scope("光固化 3D 打印", "光固化3D打印的收缩补偿模型")
        self.assertTrue(ok)

    # ---------- 成果汇总口径 ----------
    def test_summary_excludes_rejected_and_unlearned_directions(self):
        rid, entries = self._prime(quota=2, count=3)
        with patch.object(curriculum, "_run_batch", return_value=(2, 4, "")):
            curriculum._iteration()
        done_dir = curriculum._state["current"]
        # 造一个越界丢弃 + 一个从未轮到的方向
        rejected = next(e for e in entries if e["direction"] != done_dir)
        curriculum._reject(rejected, "相邻工艺")
        curriculum.stop()

        summ = curriculum.summary()
        names = [d["direction"] for d in summ["directions"]]
        self.assertEqual(names, [done_dir])
        self.assertEqual(summ["skipped_count"], 1)
        self.assertIn("丢弃越界方向 1 个", curriculum._summary_text(summ))
        last = curriculum._last_run_view()
        self.assertEqual([d["direction"] for d in last["directions"]], [done_dir])
        self.assertEqual(last["skipped_count"], 1)

    def test_busy_reason_uses_the_lightweight_active_check(self):
        from backend import tasks

        self._prime(quota=1)
        with patch.object(curriculum, "state", side_effect=AssertionError("不该拷贝整份计划")):
            self.assertIn("方向轮换", tasks.busy_reason())

    def test_replenish_judges_all_candidates_in_one_call(self):
        curriculum._state.update(seed="光固化 3D 打印", subdirection_count=3)
        calls = []

        def fake_chat(system, user, **kw):
            calls.append(user)
            return {"verdicts": [
                {"index": 1, "in_scope": True, "reason": "属于该领域"},
                {"index": 2, "in_scope": False, "reason": "相邻工艺"},
            ]}

        with patch.object(curriculum, "_split",
                          return_value=(["树脂收缩补偿模型", "FDM 支撑结构差异"], "model")), \
             patch.object(curriculum.llm, "chat_json", side_effect=fake_chat):
            self.assertTrue(curriculum._replenish(set()))
        self.assertEqual(len(calls), 1, "多条候选应合并成一次审核")
        dirs = [e["direction"] for e in curriculum._state["plan"]]
        self.assertIn("树脂收缩补偿模型", dirs)
        self.assertNotIn("FDM 支撑结构差异", dirs)
        # 结论进了缓存：再问同一个方向不会再打模型
        with patch.object(curriculum.llm, "chat_json", side_effect=AssertionError("不该再问")):
            ok, _ = curriculum.in_scope("光固化 3D 打印", "树脂收缩补偿模型")
        self.assertTrue(ok)

    # ---------- 手动跳过 ----------
    def test_skip_switches_before_the_quota_is_reached(self):
        self._prime(quota=100)
        with patch.object(curriculum, "_run_batch", return_value=(3, 2, "")):
            curriculum._iteration()          # 学 3 轮，远未满 100
        first = curriculum._state["current"]
        self.assertEqual(curriculum._state["rounds_in_current"], 3)

        with patch.object(curriculum.evolve.engine, "stop") as stop:
            res = curriculum.skip()
        self.assertTrue(res["ok"])
        self.assertTrue(curriculum._state["skip_requested"])
        stop.assert_not_called()             # 不在批次里就不用打断引擎

        with patch.object(curriculum, "_run_batch", return_value=(3, 1, "")):
            curriculum._iteration()          # 下一次迭代应当换向
        self.assertNotEqual(curriculum._state["current"], first)
        self.assertFalse(curriculum._state["skip_requested"])
        self.assertEqual(curriculum._state["switches"][-1]["reason"], "手动跳过")
        self.assertEqual(curriculum._state["total_rounds_done"], 6)

    def test_skip_during_a_batch_stops_the_engine(self):
        self._prime(quota=100)
        curriculum._state.update(in_batch=True, current=f"{SEED}：主题0")
        with patch.object(curriculum.evolve.engine, "stop") as stop:
            self.assertTrue(curriculum.skip()["ok"])
        stop.assert_called_once()

    def test_skip_is_rejected_without_a_running_task(self):
        res = curriculum.skip()
        self.assertFalse(res["ok"])
        self.assertIn("没有正在运行", res["msg"])

    def test_recover_stale_only_touches_runs_from_before_boot(self):
        old = memory.curriculum_start_run("旧任务", 1, 1, 2)
        # 模拟「上一个进程」留下的记录：开始时间早于本进程启动
        memory.curriculum_update_run(old, started_at=curriculum.BOOT_TS - 100)
        curriculum.recover_stale()
        self.assertEqual(memory.curriculum_get_run(old)["status"], "stopped")
        self.assertEqual(memory.curriculum_get_run(old)["stop_reason"], "进程重启")

        fresh = memory.curriculum_start_run("另一个进程的任务", 1, 1, 2)
        memory.curriculum_update_run(fresh, started_at=curriculum.BOOT_TS + 1)
        curriculum.recover_stale()
        self.assertEqual(memory.curriculum_get_run(fresh)["status"], "running")

    def test_old_runs_are_pruned_but_recent_ones_survive(self):
        ids = []
        for i in range(5):
            rid = memory.curriculum_start_run(f"任务{i}", 1, 1, 2)
            memory.curriculum_update_run(rid, started_at=1000 + i)
            memory.curriculum_add_directions(rid, [{"id": f"{rid}-d", "direction": "方向", "seq": 1}])
            ids.append(rid)
        self.assertEqual(memory.curriculum_prune(2), 3)
        self.assertEqual([r["id"] for r in memory.curriculum_runs(10)], [ids[4], ids[3]])
        left = memory.conn().execute("SELECT COUNT(*) FROM curriculum_directions").fetchone()[0]
        self.assertEqual(left, 2)

    def test_start_prunes_history(self):
        with patch.object(curriculum, "_loop", return_value=None), \
             patch.object(memory, "curriculum_prune", return_value=0) as prune:
            curriculum.start(SEED, rounds_per_direction=1, total_rounds=1, subdirection_count=2)
        prune.assert_called_once_with(curriculum.KEEP_RUNS)

    def test_resume_clears_stale_wait_and_failure_state(self):
        self._prime(quota=5)
        curriculum._state.update(paused=True, blocked="已有学习任务在跑，请先停止", failures=2)
        curriculum.resume()
        self.assertFalse(curriculum._state["paused"])
        self.assertEqual(curriculum._state["blocked"], "")
        self.assertEqual(curriculum._state["failures"], 0)
        self.assertEqual(curriculum._state["stage"], "learning")

    # ---------- 预览结果复用 ----------
    def test_previewed_directions_are_used_at_bootstrap(self):
        curriculum._state.update(seed=SEED, subdirection_count=3,
                                 preset=["DynaMesh 面数取舍", "Blender 拓扑重网格"])
        verdicts = {"DynaMesh 面数取舍": (True, ""), "Blender 拓扑重网格": (False, "相邻软件")}
        with patch.object(curriculum, "_split", side_effect=AssertionError("不该重新拆解")), \
             patch.object(curriculum, "_judge_many", return_value=verdicts):
            items, source = curriculum._bootstrap()
        self.assertEqual(items, ["DynaMesh 面数取舍"])
        self.assertEqual(source, "preview")
        self.assertEqual(curriculum._state["preset"], [], "预设用一次就该清掉")

    def test_bootstrap_falls_back_to_split_when_preset_is_unusable(self):
        curriculum._state.update(seed=SEED, subdirection_count=2, preset=["Blender 拓扑重网格"])
        with patch.object(curriculum, "_split", return_value=(["DynaMesh 面数取舍"], "model")), \
             patch.object(curriculum, "_judge_many", return_value={"Blender 拓扑重网格": (False, "相邻")}):
            items, source = curriculum._bootstrap()
        self.assertEqual(source, "model")
        self.assertEqual(items, ["DynaMesh 面数取舍"])

    def test_start_accepts_preset_items_from_the_api(self):
        from backend.main import app

        client = TestClient(app)
        with patch.object(curriculum, "_loop", return_value=None):
            res = client.post("/api/rotation/start", json={
                "seed": SEED, "rounds_per_direction": 1, "total_rounds": 1,
                "subdirection_count": 2, "items": ["DynaMesh 面数取舍"],
            })
        self.assertTrue(res.json()["ok"])
        self.assertEqual(curriculum._state["preset"], ["DynaMesh 面数取舍"])

    def test_summary_api_can_return_plain_text(self):
        from backend.main import app

        self._prime(quota=1, limit=2)
        with patch.object(curriculum, "_run_batch", return_value=(1, 2, "")):
            curriculum._iteration()
        curriculum.stop()

        client = TestClient(app)
        as_json = client.get("/api/rotation/summary").json()
        self.assertIn("方向轮换汇总", as_json["text"])
        self.assertTrue(as_json["summary"]["directions"])

        as_text = client.get("/api/rotation/summary?format=text")
        self.assertEqual(as_text.status_code, 200)
        self.assertTrue(as_text.headers["content-type"].startswith("text/plain"))
        self.assertIn("方向轮换汇总", as_text.text)

    def test_failure_threshold_is_configurable(self):
        self._prime(quota=5)
        config.update({"rotation_max_failures": 1})
        with patch.object(curriculum, "_run_batch", return_value=(0, 0, "模型不可用")), \
             patch.object(curriculum._wake_evt, "wait", return_value=True), \
             patch.object(curriculum.time, "sleep"):
            curriculum._iteration()
        # 阈值调成 1：第一次没推进就进入退避（默认要两次）
        self.assertEqual(curriculum._state["failures"], 1)
        self.assertEqual(curriculum._state["stage"], "waiting_model")
        self.assertFalse(curriculum._state["paused"])

    def test_summary_is_sorted_by_output_and_marks_partial_directions(self):
        rid = memory.curriculum_start_run(SEED, 10, 30, 3)
        curriculum._state.update(run_id=rid, seed=SEED, rounds_per_direction=10,
                                 total_round_limit=30, total_rounds_done=12, started_at=1.0)
        memory.curriculum_add_directions(rid, [
            {"id": "d1", "direction": f"{SEED}：少", "seq": 1},
            {"id": "d2", "direction": f"{SEED}：多", "seq": 2},
        ])
        memory.curriculum_update_direction("d1", status="stopped", rounds=2, points=1, last_round=2)
        memory.curriculum_update_direction("d2", status="done", rounds=10, points=9, last_round=12)

        summ = curriculum._build_summary()
        self.assertEqual([i["direction"] for i in summ["directions"]],
                         [f"{SEED}：多", f"{SEED}：少"])
        text = curriculum._summary_text(summ)
        self.assertIn("（未学满）", text)
        self.assertNotIn(f"{SEED}：多：10 轮 / 新增 9 条（未学满）", text)

    def test_batch_accounting_is_written_in_one_transaction(self):
        rid, entries = self._prime(quota=2)
        eid = entries[0]["id"]
        memory.curriculum_save_batch(eid, rid, rounds=2, points=3, last_round=2,
                                     sessions="[]", total_rounds=2, bogus=1)
        row = next(x for x in memory.curriculum_list_directions(rid) if x["id"] == eid)
        self.assertEqual((row["rounds"], row["points"]), (2, 3))
        self.assertEqual(memory.curriculum_get_run(rid)["total_rounds"], 2)

    def test_account_batch_uses_the_combined_write(self):
        self._prime(quota=2)
        with patch.object(curriculum, "_run_batch", return_value=(2, 1, "")), \
             patch.object(memory, "curriculum_save_batch", return_value=None) as save:
            curriculum._iteration()
        save.assert_called_once()

    def test_items_are_deduped_ignoring_spacing_and_seed_prefix(self):
        raw = {"items": ["ZBrush：DynaMesh 面数取舍", "DynaMesh面数取舍",
                         "1. 细分层级分配", "ZBrush"]}
        items = curriculum._clean_items(raw, "ZBrush", set())
        # 原文保留前缀（界面上要能看出属于哪个母方向），只按去前缀后的键去重
        self.assertEqual(items, ["ZBrush：DynaMesh 面数取舍", "细分层级分配"])

    def test_exclusion_also_uses_the_compact_key(self):
        exclude = {curriculum._dedupe_key("DynaMesh面数取舍")}
        items = curriculum._clean_items({"items": ["DynaMesh 面数取舍"]}, "ZBrush", exclude)
        self.assertEqual(items, [])

    def test_session_total_survives_detail_truncation(self):
        rid, entries = self._prime(quota=2)
        eid = entries[0]["id"]
        memory.curriculum_save_batch(eid, rid, rounds=4, points=2, last_round=4,
                                     sessions=json.dumps([{"id": f"s{i}"} for i in range(20)]),
                                     session_total=25, total_rounds=4)
        curriculum._state.update(run_id=rid, seed=SEED, rounds_per_direction=2,
                                 total_round_limit=20, total_rounds_done=4, started_at=1.0)
        summ = curriculum._build_summary()
        row = next(i for i in summ["directions"] if i["direction"] == entries[0]["direction"])
        self.assertEqual(len(row["sessions"]), 20)
        self.assertEqual(row["session_total"], 25)

    def test_skip_mid_batch_does_not_count_as_a_failure(self):
        rid, entries = self._prime(quota=5)
        curriculum._state.update(skip_requested=True, current=entries[0]["direction"],
                                 current_id=entries[0]["id"])
        curriculum._account_batch(entries[0]["id"], rid, entries[0]["direction"], 0, 0, "")
        self.assertEqual(curriculum._state["failures"], 0)
        self.assertFalse(curriculum._state["paused"])

    def test_switch_writes_are_one_transaction(self):
        rid, entries = self._prime(quota=2)
        curriculum._state.update(run_id=rid, current=entries[0]["direction"],
                                 current_id=entries[0]["id"], total_rounds_done=2)
        entries[0]["status"] = "active"
        entries[0]["rounds"] = 2
        with patch.object(curriculum, "in_scope", return_value=(True, "")), \
             patch.object(memory, "curriculum_save_switch", return_value=None) as save:
            picked = curriculum._switch("完成 2 轮")
        self.assertIsNotNone(picked)
        save.assert_called_once()
        args = save.call_args[0]
        self.assertEqual(args[0], rid)
        self.assertEqual(args[1]["id"], entries[0]["id"])       # 旧方向被关闭
        self.assertEqual(args[2]["id"], picked["id"])           # 新方向被激活
        self.assertEqual(args[3]["reason"], "完成 2 轮")

    def test_local_direction_keeps_its_prefix_and_scope_shortcut(self):
        items = curriculum._clean_items(
            {"items": ["光固化 3D 打印：质量检查清单"]}, "光固化 3D 打印", set()
        )
        self.assertEqual(items, ["光固化 3D 打印：质量检查清单"])
        with patch.object(curriculum.llm, "chat_json", side_effect=AssertionError("不该调用模型")):
            ok, _ = curriculum.in_scope("光固化 3D 打印", items[0])
        self.assertTrue(ok)

    def test_model_outage_backs_off_instead_of_pausing(self):
        self._prime(quota=5)
        with patch.object(curriculum, "_run_batch", return_value=(0, 0, "Ollama 未运行")), \
             patch.object(curriculum._wake_evt, "wait", return_value=True) as wait, \
             patch.object(curriculum.time, "sleep"):
            for _ in range(4):
                curriculum._iteration()
        self.assertFalse(curriculum._state["paused"])
        self.assertEqual(curriculum._state["stage"], "waiting_model")
        self.assertGreater(curriculum._state["retry_in"], 0)
        self.assertGreaterEqual(wait.call_count, 2, "阈值之后应进入退避等待")
        self.assertTrue(curriculum._state["active"])

    def test_persistent_outage_eventually_pauses(self):
        self._prime(quota=5)
        config.update({"rotation_max_failures": 1})
        with patch.object(curriculum, "_run_batch", return_value=(0, 0, "Ollama 未运行")), \
             patch.object(curriculum._wake_evt, "wait", return_value=True), \
             patch.object(curriculum.time, "sleep"), \
             patch.object(curriculum, "MODEL_RETRY_LIMIT", 2):
            for _ in range(3):
                curriculum._iteration()
        self.assertTrue(curriculum._state["paused"])

    def test_successful_batch_clears_the_retry_countdown(self):
        self._prime(quota=2)
        curriculum._state.update(retry_in=120, failures=3, stage="waiting_model")
        with patch.object(curriculum, "_run_batch", return_value=(1, 1, "")):
            curriculum._iteration()
        self.assertEqual(curriculum._state["retry_in"], 0)
        self.assertEqual(curriculum._state["failures"], 0)

    def test_status_text_reflects_the_waiting_stage(self):
        self._prime(quota=5)
        curriculum._state.update(stage="waiting_model", retry_in=60)
        snap = curriculum.state()
        self.assertEqual(snap["status_text"], "等待模型")
        self.assertEqual(snap["retry_in"], 60)
