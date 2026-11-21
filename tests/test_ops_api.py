"""「证据复核与检索运维」面板的前后端契约 + startup 注册回归。

这一组测试针对的是两类**不会让 Python 报错、却会让功能静默失效**的缺陷：

1. 装饰器粘连：``@app.on_event("startup")`` 若被插到路由上方，会和
   ``@app.get(...)`` 组成装饰器链，把 route 函数注册成 startup 回调，
   而真正的 ``_startup()`` 反而没挂上——书库初始化、僵尸状态恢复、
   调度器启动全部丢失，启动时还平白多跑一次 256 MB 库的只读盘点。
2. 前端调用点漂移：``opsPost(path, body)`` 改成 ``opsPost(body)`` 后，
   旧调用点仍传路径字符串，``Object.assign`` 会把字符串展开成字符键，
   ``dry_run`` / ``batch`` / ``limit`` 悄悄丢失，预演可能变成真写库。

契约验证刻意不启动 uvicorn、也不进入 TestClient 的 lifespan：直接调用
route 函数（同步、主线程、临时库），再用路由表核对 method/path，最后用
``json.dumps`` 确认返回值可序列化。这样既验证了参数名与默认值，又不把
跨线程 SQLite 句柄带进测试夹具。
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from test_regressions import IsolatedTest
from backend import config, llm, main, memory, review_pipeline, search_index

WEB = Path(config.WEB)

# 前端面板实际会请求的端点（method, path）
OPS_ROUTES = {
    ("GET", "/api/analytics/evidence-policy"),
    ("GET", "/api/review/plan"),
    ("POST", "/api/review/enqueue"),
    ("POST", "/api/review/run-local"),
    ("POST", "/api/review/run-urls"),
    ("GET", "/api/review/status"),
    ("GET", "/api/search/index"),
    ("POST", "/api/search/index/rebuild"),
    ("GET", "/api/knowledge/hybrid-search"),
    ("GET", "/api/events/rollups"),
    ("GET", "/api/events/alerts"),
    ("POST", "/api/events/archive"),
    ("GET", "/api/topics/clusters"),
    ("GET", "/api/topics/vocabulary"),
    ("POST", "/api/topics/vocabulary"),
    ("POST", "/api/topics/aliases"),
    ("POST", "/api/topics/reclassify"),
}


class OpsStartupTests(IsolatedTest):
    def test_startup_registers_real_handler_not_a_route(self):
        """startup 只应挂 _startup；route 函数不能被装饰器链误注册。"""
        names = [getattr(f, "__name__", "") for f in main.app.router.on_startup]
        self.assertIn("_startup", names)
        self.assertNotIn("api_review_plan", names)
        # 所有 startup handler 都必须是无参可调用的
        for f in main.app.router.on_startup:
            self.assertEqual(len([
                p for p in inspect.signature(f).parameters.values()
                if p.default is p.empty
            ]), 0, f"{getattr(f, '__name__', f)} 带必填参数，启动时会 TypeError")

    def test_all_ops_panel_routes_are_registered(self):
        # 路由已按域拆进 backend/routers；新版 FastAPI 的 include_router 不再摊平
        # app.routes，必须经 iter_api_routes 展开，否则会把全部接口误报为未注册。
        from backend.routers import iter_api_routes

        registered = {
            (m, r.path)
            for r in iter_api_routes(main.app)
            for m in (getattr(r, "methods", None) or set())
        }
        missing = OPS_ROUTES - registered
        self.assertFalse(missing, f"前端面板会打但后端没注册的端点：{sorted(missing)}")

    def test_frontend_ops_post_calls_pass_no_path_argument(self):
        """opsPost 只接收 body；旧式 opsPost("/api/...", {...}) 会把参数弄丢。"""
        text = (WEB / "app.js").read_text(encoding="utf-8")
        calls = re.findall(r"opsPost\(([^)]*)\)", text)
        self.assertTrue(calls, "没找到 opsPost 调用点")
        for args in calls:
            self.assertNotIn("/api/", args,
                             f"opsPost 仍在传路径参数：opsPost({args})")
        self.assertIn("dry_run: !opsWriteEnabled()", text,
                      "opsPost 必须把预演开关注入每个 POST body")

    def test_frontend_button_ids_match_js_bindings(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        js = (WEB / "app.js").read_text(encoding="utf-8")
        in_html = set(re.findall(r'id="(btn-ops-[a-z-]+)"', html))
        in_js = set(re.findall(r'on\("#(btn-ops-[a-z-]+)"', js))
        self.assertTrue(in_html, "页面里没有运维面板按钮")
        self.assertEqual(in_html, in_js,
                         f"按钮与绑定不一致：只在 HTML={sorted(in_html - in_js)} "
                         f"只在 JS={sorted(in_js - in_html)}")


class OpsEndpointTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        search_index._available = None
        # 造两条有向量的点，让 plan / hybrid / clusters 都有真实数据可查
        for i, claim in enumerate(("ZBrush 4R6 DynaMesh 分辨率 128",
                                   "Cura 切片层高 0.2mm 与填充率")):
            memory.add_point(topic="3D 打印", claim=claim, detail="历史无引文知识",
                             vec=[1.0, 0.0], dedup=False, source_kind="book",
                             book_id=f"bk{i}", source_url=f"file:///K:/books/{i}.pdf")
        search_index.rebuild()

    def _jsonable(self, value):
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def test_read_endpoints_return_serializable_payloads(self):
        """按前端实际调用方式直接调 route 函数，全部可 JSON 序列化。"""
        with patch.object(llm, "embed", side_effect=lambda xs: [[1.0, 0.0] for _ in xs]):
            payloads = {
                "evidence-policy": main.api_evidence_policy(),
                "review-plan": main.api_review_plan(sample=5),
                "review-status": main.api_review_status(),
                "index-status": main.api_search_index_status(),
                "hybrid": main.api_knowledge_hybrid_search(q="ZBrush", k=8, alpha=0.5, topic=None),
                "rollups": main.api_events_rollups(days=7),
                "alerts": main.api_events_alerts(limit=20),
                "clusters": main.api_topics_clusters(limit=40),
                "vocabulary": main.api_topics_vocabulary(),
            }
        for name, payload in payloads.items():
            with self.subTest(endpoint=name):
                self.assertIsInstance(payload, dict)
                self.assertTrue(self._jsonable(payload) is not None)
        self.assertTrue(payloads["review-plan"]["read_only"])
        self.assertIn("no_evidence_total", payloads["review-plan"])
        self.assertIn("independent_verification", payloads["evidence-policy"])
        self.assertEqual(payloads["hybrid"]["method"], "hybrid_rrf")

    def test_all_write_endpoints_default_to_dry_run_and_touch_nothing(self):
        """前端不勾「真正执行」时，入队/补证不写库，URL 复检不发请求。"""
        with patch.object(review_pipeline, "_probe") as probe:
            out = {
                "enqueue": main.api_review_enqueue({}),
                "run-local": main.api_review_run_local({}),
                "run-urls": main.api_review_run_urls({}),
                "archive": main.api_events_archive({}),
            }
            probe.assert_not_called()
        for name, payload in out.items():
            with self.subTest(endpoint=name):
                self.assertTrue(payload.get("dry_run"), f"{name} 默认不是预演：{payload}")
        c = memory.conn()
        queued = int(c.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0])
        self.assertEqual(queued, 0, "预演入队竟然写了 review_queue")
        # 显式传 body 的字段名必须被后端认（否则前端参数会静默失效）
        with patch.object(review_pipeline, "_probe") as probe:
            local = main.api_review_run_local({"batch": 50, "dry_run": True})
            urls = main.api_review_run_urls({"limit": 20, "dry_run": True})
            enq = main.api_review_enqueue({"dry_run": True, "limit": 10})
            probe.assert_not_called()
        self.assertEqual(local["examined"], 0)
        self.assertEqual(urls["distinct_urls"], 0)
        self.assertTrue(enq["dry_run"])

    def test_explicit_execute_enqueues_but_never_autoverifies(self):
        """勾选真正执行后能入队；补证命中也只写 legacy_literal，不改 status。"""
        enq = main.api_review_enqueue({"dry_run": False})
        self.assertFalse(enq["dry_run"])
        self.assertGreater(enq.get("queued", 0), 0)
        st = main.api_review_status()
        self.assertTrue(st["available"])
        self.assertGreater(sum(v for d in st["queue"].values() for v in d.values()), 0)

        local = main.api_review_run_local({"dry_run": False, "batch": 50})
        self.assertFalse(local["dry_run"])
        c = memory.conn()
        rows = [dict(r) for r in c.execute(
            "SELECT status,verification FROM knowledge "
            "WHERE COALESCE(evidence,'')<>''")]
        for r in rows:
            self.assertNotEqual(r["verification"], "verified")
            self.assertNotEqual(r["status"], "verified")

    def test_plan_is_read_only_on_unmigrated_shape(self):
        """plan 走 mode=ro，不触发迁移：调用前后主库列集合不变。"""
        c = memory.conn()
        before = {r[1] for r in c.execute("PRAGMA table_info(knowledge)")}
        out = review_pipeline.plan(sample=2)
        after = {r[1] for r in memory.conn().execute("PRAGMA table_info(knowledge)")}
        self.assertTrue(out["available"])
        self.assertEqual(before, after)
        self.assertEqual(out["read_only"], True)

    def test_http_layer_accepts_exactly_the_frontend_payloads(self):
        """按前端 app.js 的真实 path/body 打一遍，杜绝 422 / 500 / 参数静默丢失。"""
        # 不进入 with：不跑 startup，避免真实网络探测与调度器启动
        client = TestClient(main.app)
        calls = [
            ("get", "/api/analytics/evidence-policy", None),
            ("get", "/api/review/plan?sample=5", None),
            ("post", "/api/review/enqueue", {"dry_run": True}),
            ("post", "/api/review/run-local", {"dry_run": True, "batch": 50}),
            ("post", "/api/review/run-urls", {"dry_run": True, "limit": 20}),
            ("get", "/api/review/status", None),
            ("get", "/api/search/index", None),
            ("get", "/api/knowledge/hybrid-search?q=ZBrush&k=8", None),
            ("get", "/api/events/rollups", None),
            ("get", "/api/topics/clusters", None),
        ]
        with patch.object(llm, "embed", side_effect=lambda xs: [[1.0, 0.0] for _ in xs]), \
             patch.object(review_pipeline, "_probe") as probe:
            for method, path, body in calls:
                with self.subTest(path=path):
                    fn = getattr(client, method)
                    r = fn(path, json=body) if body is not None else fn(path)
                    self.assertEqual(r.status_code, 200,
                                     f"{path} -> HTTP {r.status_code}: {r.text[:200]}")
                    self.assertIsInstance(r.json(), (dict, list))
            probe.assert_not_called()
        # 前端漏传 body 时也不能 422：后端必须回落成只读预演
        r = client.post("/api/review/enqueue")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(r.json()["dry_run"])
