"""main.py 拆分为 backend/routers/* 之后的结构性回归。

拆分把 117 个接口从一个 1500 行的 main.py 挪进 14 个按域划分的 APIRouter。
Starlette 按注册顺序匹配，所以「挪位置」本身就可能让某个接口被别的路由
吞掉（经典案例：/api/knowledge/{pid} 先于 /api/knowledge/search 注册，
"search" 就会被当成知识点 id）。这里把这类不会报错、却会让接口静默失效的
缺陷固化成测试：

1. 同一 (method, path) 不得重复注册；
2. 任何路由都不能被更早注册的路由完全遮蔽（遮蔽 = 后者能命中的每个路径
   前者也都能命中）；
3. 不同域模块之间不允许出现同方法、可同时命中同一路径的路由——跨模块的
   先后顺序因此与匹配结果无关，调整 ROUTERS 顺序不会改变行为；
4. main 只做装配，不再定义业务接口；且为测试/基准脚本保留 main.api_xxx 兼容导出；
5. 经 HTTP 打一遍每个域的只读接口，确认挂载后可达。
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from test_regressions import IsolatedTest
from backend import main
from backend.routers import ROUTERS, iter_api_routes

ROOT = Path(__file__).resolve().parent.parent


def _api_routes():
    out = []
    for r in iter_api_routes(main.app):
        methods = getattr(r, "methods", None) or set()
        endpoint = getattr(r, "endpoint", None)
        if not methods or endpoint is None or r.path.startswith(("/docs", "/redoc", "/openapi")):
            continue
        out.append((r.path, frozenset(methods), endpoint))
    return out


def _seg_regex(seg: str) -> str:
    out, pos = "", 0
    for m in re.finditer(r"\{[^}]+\}", seg):
        out += re.escape(seg[pos:m.start()]) + "[^/]+"
        pos = m.end()
    return "^" + out + re.escape(seg[pos:]) + "$"


def _overlap(a: str, b: str) -> bool:
    """两个路径模板是否存在同时命中的具体路径（逐段近似，参数段按 [^/]+）。"""
    sa, sb = a.split("/"), b.split("/")
    if len(sa) != len(sb):
        return False
    for x, y in zip(sa, sb):
        px, py = "{" in x, "{" in y
        if not px and not py and x != y:
            return False
        if px and not py and not re.match(_seg_regex(x), y):
            return False
        if py and not px and not re.match(_seg_regex(y), x):
            return False
    return True


def _covers(a: str, b: str) -> bool:
    """a 是否能命中 b 能命中的全部路径（b 被 a 完全遮蔽）。"""
    sa, sb = a.split("/"), b.split("/")
    if len(sa) != len(sb):
        return False
    for x, y in zip(sa, sb):
        if x == y:
            continue
        if x.startswith("{") and x.endswith("}") and x.count("{") == 1:
            continue  # a 的整段参数能吃下 b 的任何单段
        return False
    return True


class RouterStructureTests(unittest.TestCase):
    def test_every_domain_router_is_mounted(self):
        mods = {getattr(r[2], "__module__", "") for r in _api_routes()}
        for router in ROUTERS:
            self.assertTrue(router.routes, "空的 APIRouter 不该登记进 ROUTERS")
            mod = router.routes[0].endpoint.__module__
            self.assertIn(mod, mods, f"{mod} 的路由没有挂到 app 上")

    def test_no_duplicate_method_path(self):
        seen = {}
        for path, methods, endpoint in _api_routes():
            for m in methods:
                key = (m, path)
                self.assertNotIn(key, seen, f"{m} {path} 重复注册：{seen.get(key)} / {endpoint.__name__}")
                seen[key] = endpoint.__name__

    def test_no_route_is_shadowed_by_an_earlier_one(self):
        routes = _api_routes()
        for i, (pa, ma, ea) in enumerate(routes):
            for pb, mb, eb in routes[i + 1:]:
                if ma & mb and _covers(pa, pb):
                    self.fail(f"{sorted(ma & mb)} {pb}（{eb.__name__}）永远被先注册的 "
                              f"{pa}（{ea.__name__}）吞掉，字面量路由要声明在参数路由之前")

    def test_routes_of_different_domains_never_overlap(self):
        routes = _api_routes()
        for i, (pa, ma, ea) in enumerate(routes):
            for pb, mb, eb in routes[i + 1:]:
                if ea.__module__ == eb.__module__ or not (ma & mb):
                    continue
                if _overlap(pa, pb):
                    self.fail(f"跨模块路由重叠：{ea.__module__}:{pa} 与 {eb.__module__}:{pb}"
                              f"（{sorted(ma & mb)}），匹配结果将依赖 ROUTERS 顺序")

    def test_main_only_assembles(self):
        tree = ast.parse((ROOT / "backend" / "main.py").read_text(encoding="utf-8"))
        paths = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and getattr(d.func.value, "id", "") == "app"
                            and d.func.attr in {"get", "post", "put", "patch", "delete"}):
                        paths.append(ast.literal_eval(d.args[0]))
        self.assertEqual(paths, ["/"], "业务接口请放进 backend/routers/<域>.py，main 只负责装配")

    def test_main_reexports_every_route_function(self):
        for _path, _methods, endpoint in _api_routes():
            name = endpoint.__name__
            if not name.startswith("api_"):
                continue
            self.assertIs(getattr(main, name, None), endpoint,
                          f"main.{name} 缺失或指向了别的函数（基准脚本 / 测试按 main.api_xxx 直接调用）")

    def test_shadowed_name_is_resolved_without_changing_route_name(self):
        """原 main.py 里两个 api_lib_parse 互相遮蔽；拆分后函数改名，路由名保持不变。"""
        by_path = {(p, tuple(sorted(m))): e for p, m, e in _api_routes()}
        names = {(r.path, r.name) for r in iter_api_routes(main.app) if getattr(r, "methods", None)}
        self.assertIn(("/api/library/books/{bid}/parse", "api_lib_parse"), names)
        self.assertIn(("/api/library/parse", "api_lib_parse"), names)
        self.assertIs(by_path[("/api/library/parse", ("POST",))], main.api_lib_parse)


class RouterHttpSmokeTests(IsolatedTest):
    """每个域挑只读接口打一遍：挂载后 HTTP 可达，且没有被别的路由误吞。"""

    def test_one_readonly_endpoint_per_domain(self):
        client = TestClient(main.app)  # 不进入 with：不跑 startup
        calls = [
            "/api/config",                       # system
            "/api/rotation",                     # learning
            "/api/events/recent?limit=5",        # events
            "/api/knowledge/cleanup",            # knowledge（字面量，不能被 {pid} 吞）
            "/api/knowledge/duplicates?limit=5",  # knowledge
            "/api/perf",                         # analytics
            "/api/topics/vocabulary",            # topics
            "/api/backups",                      # backups
            "/api/library/state",                # library
            "/api/ocr/state",                    # ocr
            "/api/review/status",                # review
        ]
        for path in calls:
            with self.subTest(path=path):
                r = client.get(path, headers={"host": "127.0.0.1"})
                self.assertEqual(r.status_code, 200, f"{path} → {r.status_code} {r.text[:200]}")
                self.assertEqual(r.headers.get("cache-control"), "no-store")
        r = client.get("/api/knowledge/not-a-real-id", headers={"host": "127.0.0.1"})
        self.assertEqual(r.status_code, 404)

    def test_train_accepts_dataset_built_by_finetune_and_rejects_escape(self):
        """「构建数据集」写在 finetune.OUTPUTS；训练接口必须放行它，同时仍拒绝目录逃逸。"""
        from unittest.mock import patch
        from backend import config, finetune

        client = TestClient(main.app)
        out = Path(config.DATA) / "finetune"
        out.mkdir(parents=True, exist_ok=True)
        ds = out / "dataset_demo_1.jsonl"
        ds.write_text('{"instruction":"q","output":"a"}\n', encoding="utf-8")
        with patch.object(finetune, "OUTPUTS", out), \
             patch.object(finetune, "train_async", return_value={"ok": True}) as train:
            r = client.post("/api/finetune/train", json={"dataset_path": str(ds)},
                            headers={"host": "127.0.0.1"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(train.call_args[0][0], str(ds.resolve()))
            outside = Path(config.DATA).parent / "evil.jsonl"
            r = client.post("/api/finetune/train", json={"dataset_path": str(outside)},
                            headers={"host": "127.0.0.1"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("路径不在允许的目录内", r.text)


if __name__ == "__main__":
    unittest.main()
