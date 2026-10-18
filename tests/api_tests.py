"""API 层测试：参数校验、异常输入、并发。

需要服务在运行：http://127.0.0.1:8765
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8765"
rows: list[dict] = []


def ck(name: str, fn):
    t0 = time.time()
    try:
        note = fn()
        rows.append({"n": name, "s": "PASS", "t": time.time() - t0, "m": str(note)[:100]})
    except AssertionError as e:
        rows.append({"n": name, "s": "FAIL", "t": time.time() - t0, "m": str(e)[:120]})
    except Exception as e:  # noqa: BLE001
        rows.append({"n": name, "s": "FAIL", "t": time.time() - t0,
                     "m": f"{type(e).__name__}: {str(e)[:100]}"})


def get(path: str, **kw) -> httpx.Response:
    return httpx.get(BASE + path, timeout=30, **kw)


def post(path: str, body=None, **kw) -> httpx.Response:
    return httpx.post(BASE + path, json=body or {}, timeout=120, **kw)


def main() -> int:
    try:
        get("/api/status")
    except Exception as e:  # noqa: BLE001
        print(f"服务未运行：{e}")
        return 2

    def status_ok():
        r = get("/api/status")
        assert r.status_code == 200, r.status_code
        d = r.json()
        assert "stats" in d and "engine" in d
        return f"{d['stats']['points']} 知识点"

    def knowledge_negative_limit():
        r = get("/api/knowledge?limit=-1")
        assert r.status_code == 200, r.status_code
        n = len(r.json())
        assert n <= 1000, f"负数 limit 未夹紧，返回 {n} 条"
        return f"{n} 条"

    def knowledge_huge_limit():
        r = get("/api/knowledge?limit=999999")
        assert r.status_code == 200
        n = len(r.json())
        assert n <= 1000, f"超大 limit 未夹紧，返回 {n} 条"
        return f"{n} 条"

    def books_negative_limit():
        r = get("/api/library/books?limit=-5")
        assert r.status_code == 200
        n = len(r.json()["items"])
        assert n <= 1000, f"未夹紧，返回 {n} 条"
        return f"{n} 条"

    def book_missing_404():
        r = get("/api/library/books/__nope__")
        assert r.status_code == 404, f"应 404，实际 {r.status_code}"
        return "404"

    def page_render_out_of_range():
        books = get("/api/library/books?fmt=pdf&limit=1").json()["items"]
        if not books:
            return "无 PDF 样本，跳过"
        r = get(f"/api/library/books/{books[0]['id']}/page/999999.png")
        assert r.status_code == 404, f"越界页应 404，实际 {r.status_code}"
        return "404"

    def add_dir_empty():
        r = post("/api/library/dirs", {"path": ""})
        assert r.status_code == 400, f"空路径应 400，实际 {r.status_code}"
        return "400"

    def add_dir_missing():
        r = post("/api/library/dirs", {"path": "Z:\\definitely\\not\\exist"})
        assert r.status_code == 200
        assert not r.json()["ok"], "不存在的目录竟然添加成功"
        return r.json()["msg"][:40]

    def learn_empty_direction():
        r = post("/api/learn/start", {"direction": ""})
        assert r.status_code == 400, f"空方向应 400，实际 {r.status_code}"
        return "400"

    def chat_empty_question():
        r = post("/api/chat", {"question": ""})
        assert r.status_code == 400, f"空问题应 400，实际 {r.status_code}"
        return "400"

    def import_empty_ids():
        r = post("/api/library/import", {"book_ids": []})
        assert r.status_code == 400, f"空 ids 应 400，实际 {r.status_code}"
        return "400"

    def import_url_bad_scheme():
        r = post("/api/library/import-url", {"url": "ftp://x/y"})
        assert r.status_code == 200
        assert not r.json()["ok"], "非法协议竟然通过"
        return r.json()["msg"][:40]

    def ocr_bad_page():
        books = get("/api/library/books?fmt=pdf&limit=1").json()["items"]
        if not books:
            return "无 PDF 样本"
        r = get(f"/api/library/books/{books[0]['id']}/ocr/999999")
        assert r.status_code == 404, f"应 404，实际 {r.status_code}"
        return "404"

    def ocr_state_shape():
        r = get("/api/ocr/state")
        assert r.status_code == 200
        d = r.json()
        for k in ("running", "total", "done", "engine"):
            assert k in d, f"缺少 {k}"
        return "字段完整"

    def events_recent_negative():
        r = get("/api/events/recent?limit=-3")
        assert r.status_code == 200
        return f"{len(r.json())} 条"

    def concurrent_status():
        def one(_):
            return get("/api/status").status_code

        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            codes = list(ex.map(one, range(20)))
        assert all(c == 200 for c in codes), f"并发有非 200：{set(codes)}"
        return "20 并发全部 200"

    def concurrent_mixed():
        paths = ["/api/status", "/api/knowledge?limit=5", "/api/library/stats",
                 "/api/library/books?limit=5", "/api/ocr/state"]

        def one(i):
            return get(paths[i % len(paths)]).status_code

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            codes = list(ex.map(one, range(24)))
        bad = [c for c in codes if c != 200]
        assert not bad, f"混合并发有异常：{set(bad)}"
        return "24 次混合请求全部 200"

    def long_query():
        # 前端 maxlength=200，这里测 2.5 倍长度仍能正常处理。
        # 超过约 2000 字会撞上 HTTP 服务器的 URL 长度上限（连接被直接断开），
        # 属已知限制，靠前端 maxlength 兜住。
        q = "测试拓扑细分策略" * 60
        r = get("/api/knowledge", params={"q": q})
        assert r.status_code == 200, f"长查询异常 {r.status_code}"
        return f"{len(q)} 字，{r.status_code}"

    ck("状态接口", status_ok)
    ck("知识点 limit=-1", knowledge_negative_limit)
    ck("知识点 limit=999999", knowledge_huge_limit)
    ck("书目 limit=-5", books_negative_limit)
    ck("不存在的书目 404", book_missing_404)
    ck("越界页渲染 404", page_render_out_of_range)
    ck("添加空路径目录 400", add_dir_empty)
    ck("添加不存在目录", add_dir_missing)
    ck("学习空方向 400", learn_empty_direction)
    ck("空问题 400", chat_empty_question)
    ck("导入空 ids 400", import_empty_ids)
    ck("非法协议链接", import_url_bad_scheme)
    ck("OCR 越界页 404", ocr_bad_page)
    ck("OCR 状态字段", ocr_state_shape)
    ck("事件 limit=-3", events_recent_negative)
    ck("20 并发状态请求", concurrent_status)
    ck("24 次混合并发", concurrent_mixed)
    ck("超长查询字符串", long_query)

    print("=" * 70)
    print("  API 测试结果")
    print("=" * 70)
    for r in rows:
        icon = "✓" if r["s"] == "PASS" else "✗"
        print(f"  {icon} {r['n']:<24} {r['t']*1000:>7.0f}ms  {r['m']}")
    p = sum(1 for r in rows if r["s"] == "PASS")
    f = len(rows) - p
    print("=" * 70)
    print(f"  通过 {p} · 失败 {f} · 共 {len(rows)}")
    print("=" * 70)
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
