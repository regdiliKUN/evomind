"""EvoMind 测试套件：正常路径 / 边界条件 / 异常场景。

用法：
    .venv\\Scripts\\python.exe tests\\run_tests.py          # 全部
    .venv\\Scripts\\python.exe tests\\run_tests.py --fast   # 跳过网络与 LLM

设计原则：
  - 不污染用户数据：边界用例用临时目录和临时 SQLite
  - 每个用例独立捕获异常，一个失败不影响其他
  - 记录耗时，便于发现性能瓶颈
"""
from __future__ import annotations

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# 测试一律跑在临时数据库上。
# 以前这里直接连 data/knowledge/knowledge.db，每跑一次就往用户知识库里
# 塞一批 __test_* / __perf 知识点（实测累积了 84 条，总览页的主题列表
# 前几名全是测试残留）。必须在导入 backend 之后、任何 conn() 之前改掉。
# ---------------------------------------------------------------------------
_TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="evo_db_"))
import backend.config as _config  # noqa: E402

_config.DB_FILE = _TMP_DB_DIR / "knowledge.db"

FAST = "--fast" in sys.argv

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results: list[dict] = []


def case(group: str, name: str, fn, expect_error: bool = False):
    """跑一个用例。expect_error=True 表示「应该优雅失败而不是崩溃」。"""
    t0 = time.time()
    try:
        r = fn()
        el = time.time() - t0
        if expect_error:
            _results.append(
                {"group": group, "name": name, "status": WARN, "elapsed": el,
                 "note": f"预期报错但正常返回：{str(r)[:70]}"}
            )
        else:
            _results.append(
                {"group": group, "name": name, "status": PASS, "elapsed": el,
                 "note": str(r)[:110]}
            )
    except Exception as e:  # noqa: BLE001
        el = time.time() - t0
        if expect_error:
            _results.append(
                {"group": group, "name": name, "status": PASS, "elapsed": el,
                 "note": f"已优雅拒绝：{type(e).__name__}: {str(e)[:60]}"}
            )
        else:
            _results.append(
                {"group": group, "name": name, "status": FAIL, "elapsed": el,
                 "note": f"{type(e).__name__}: {str(e)[:90]}",
                 "trace": traceback.format_exc()[-600:]}
            )
    return _results[-1]


# ==========================================================================
# 1. 配置层
# ==========================================================================
def test_config():
    from backend import config

    def defaults_exist():
        d = config.DEFAULTS
        need = ["model", "embed_model", "min_relevance", "dedup_threshold",
                "fetch_per_round", "queries_per_round", "ollama_url",
                "vision_model", "ocr_dpi", "disk_budget_gb"]
        missing = [k for k in need if k not in d]
        assert not missing, f"缺少默认项 {missing}"
        return f"{len(d)} 个配置项"

    def get_default():
        assert config.get("min_relevance") is not None
        return config.get("min_relevance")

    def get_missing_key():
        # 未定义的键应返回 None 而不是崩溃
        assert config.get("__not_exist__") is None
        return "返回 None"

    def update_roundtrip():
        old = config.get("idle_minutes")
        config.update({"idle_minutes": 33})
        assert config.get("idle_minutes") == 33
        config.update({"idle_minutes": old})
        return "读写一致"

    def update_unknown_key():
        # 写未定义的键不应该污染配置
        config.update({"__bogus__": 1})
        return f"未定义键={'已忽略' if config.get('__bogus__') is None else '被写入'}"

    case("配置", "默认配置项完整", defaults_exist)
    case("配置", "读取默认值", get_default)
    case("配置", "读取不存在的键", get_missing_key)
    case("配置", "更新后立即生效", update_roundtrip)
    case("配置", "写入未定义键", update_unknown_key)


# ==========================================================================
# 2. 记忆层
# ==========================================================================
def test_memory():
    from backend import memory

    marker = f"__test_{int(time.time()*1000)}"

    def add_point_normal():
        # dedup=False：这里只测「写入本身」，去重另有用例
        pid = memory.add_point(
            topic=f"{marker}_topic", claim=f"{marker} 这是一条用于测试的知识点陈述内容",
            detail="测试细节", source_url="http://test.local/1",
            source_title="测试来源", source_kind="web", tags=["测试"],
            confidence=0.9, session_id="", dedup=False,
        )
        assert pid, "add_point 返回空"
        return f"id={pid}"

    def add_point_too_short():
        # 过短的 claim 应被拒绝（防止垃圾入库）
        pid = memory.add_point(
            topic=marker, claim="短", detail="", source_url="", source_title="",
            source_kind="web", tags=[], confidence=0.5, session_id="",
        )
        assert pid is None, "过短内容未被拒绝"
        return "已拒绝"

    def dedup_identical():
        claim = f"{marker} 重复检测用的完全相同的一句话内容测试"
        a = memory.add_point(topic=marker, claim=claim, detail="", source_url="",
                             source_title="", source_kind="web", tags=[],
                             confidence=0.9, session_id="")
        b = memory.add_point(topic=marker, claim=claim, detail="", source_url="",
                             source_title="", source_kind="web", tags=[],
                             confidence=0.9, session_id="")
        assert b is None, "完全重复的内容未被去重"
        return f"首条 {a}，重复被拒"

    def search_returns_list():
        r = memory.search("测试知识点", k=3)
        assert isinstance(r, list)
        return f"{len(r)} 条"

    def search_empty_query():
        r = memory.search("", k=3)
        assert isinstance(r, list)
        return f"空查询返回 {len(r)} 条（未崩溃）"

    def search_k_zero():
        r = memory.search("测试", k=0)
        assert isinstance(r, list) and len(r) == 0
        return "k=0 返回空列表"

    def stats_shape():
        s = memory.stats()
        for k in ("points", "documents", "resources", "sessions", "new_24h"):
            assert k in s, f"stats 缺少 {k}"
        return f"{s['points']} 知识点"

    def doc_seen_unknown():
        assert memory.doc_seen("http://never.visited/xyz") is False
        return "未访问返回 False"

    def add_document_and_seen():
        u = f"http://test.local/{marker}"
        memory.mark_doc(u, "标题", "测试方向", 500, True)
        assert memory.doc_seen(u) is True
        return "写入后可查到"

    def concurrent_writes():
        """多线程并发写入 —— 检验 threading.local 连接是否真的安全。"""
        errs: list[str] = []

        def worker(i: int):
            try:
                for j in range(6):
                    memory.add_point(
                        topic=f"{marker}_conc",
                        claim=f"{marker} 并发写入测试第 {i} 线程第 {j} 条内容各不相同",
                        detail="", source_url="", source_title="",
                        source_kind="web", tags=[], confidence=0.8,
                        session_id="", dedup=False,
                    )
            except Exception as e:  # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errs, f"并发写入出错 {errs[:2]}"
        return "6 线程 × 6 条，无异常"

    def recent_events_limit():
        memory.log_event("info", f"{marker} 事件")
        evts = memory.recent_events(limit=5)
        assert isinstance(evts, list) and len(evts) <= 5
        return f"{len(evts)} 条"

    case("记忆", "写入正常知识点", add_point_normal)
    case("记忆", "拒绝过短内容", add_point_too_short)
    case("记忆", "完全重复被去重", dedup_identical)
    case("记忆", "语义检索返回列表", search_returns_list)
    case("记忆", "空字符串检索", search_empty_query)
    case("记忆", "k=0 边界", search_k_zero)
    case("记忆", "统计字段完整", stats_shape)
    case("记忆", "未访问 URL 判定", doc_seen_unknown)
    case("记忆", "文档写入与查重", add_document_and_seen)
    case("记忆", "多线程并发写入", concurrent_writes)
    case("记忆", "事件流读取", recent_events_limit)


# ==========================================================================
# 3. 检索层（纯函数部分，不联网）
# ==========================================================================
def test_search_pure():
    from backend import search

    def en_tokens():
        t = search._en_tokens("ZBrush DynaMesh the and retopology")
        assert "zbrush" in t and "the" not in t
        return f"{sorted(t)}"

    def should_arxiv_yes():
        assert search.should_use_arxiv("RAG reranking cross-encoder benchmark")
        return "学术方向 → 查 arXiv"

    def should_arxiv_no():
        assert not search.should_use_arxiv("ZBrush DynaMesh 面数取舍")
        return "软件方向 → 跳过 arXiv"

    def should_arxiv_mesh_trap():
        # mesh/generation 这类词不该触发 arXiv（会拿回同名不同义的论文）
        assert not search.should_use_arxiv("3D mesh generation sculpting workflow")
        return "mesh/generation 不误触发"

    def simplify_query():
        c = search._simplify_query("ZBrush DynaMesh vs ZRemesher polygon count")
        assert "vs" not in c[0], f"vs 未过滤：{c}"
        return f"{c}"

    def simplify_empty():
        c = search._simplify_query("")
        assert c
        return f"{c}"

    def simplify_chinese_only():
        c = search._simplify_query("中文查询没有英文词")
        assert c
        return f"{c}"

    def pick_se_site():
        assert search._pick_se_site("zbrush retopology") == "blender"
        assert search._pick_se_site("resin print support") == "3dprinting"
        assert search._pick_se_site("完全无关的中文") == "stackoverflow"
        return "站点选择正确"

    def parse_pages_normal():
        from backend import ocr
        p = ocr.parse_pages("1-5,8", 20)
        assert p == [0, 1, 2, 3, 4, 7], p
        return f"{p}"

    def parse_pages_overflow():
        from backend import ocr
        p = ocr.parse_pages("18-999", 20)
        assert p == [17, 18, 19], p
        return "越界被裁剪"

    def parse_pages_reverse():
        from backend import ocr
        p = ocr.parse_pages("9-7", 20)
        assert p == [6, 7, 8], f"倒序区间未处理：{p}"
        return f"{p}"

    def parse_pages_negative():
        from backend import ocr
        p = ocr.parse_pages("-5", 20)
        assert all(x >= 0 for x in p), f"出现负数页：{p}"
        return f"{p}"

    def parse_pages_garbage():
        from backend import ocr
        p = ocr.parse_pages("abc,?,1-", 20)
        assert isinstance(p, list)
        return f"{p}"

    def parse_pages_zero_total():
        from backend import ocr
        p = ocr.parse_pages("all", 0)
        assert p == []
        return "0 页返回空"

    case("检索", "英文词提取与停用词过滤", en_tokens)
    case("检索", "学术方向启用 arXiv", should_arxiv_yes)
    case("检索", "软件方向跳过 arXiv", should_arxiv_no)
    case("检索", "mesh 类词不误触发 arXiv", should_arxiv_mesh_trap)
    case("检索", "查询简化去掉 vs", simplify_query)
    case("检索", "空查询简化", simplify_empty)
    case("检索", "纯中文查询简化", simplify_chinese_only)
    case("检索", "StackExchange 站点选择", pick_se_site)
    case("OCR", "页码解析常规", parse_pages_normal)
    case("OCR", "页码越界裁剪", parse_pages_overflow)
    case("OCR", "页码倒序区间", parse_pages_reverse)
    case("OCR", "页码负数输入", parse_pages_negative)
    case("OCR", "页码垃圾输入", parse_pages_garbage)
    case("OCR", "总页数为 0", parse_pages_zero_total)


# ==========================================================================
# 4. 书库层（用临时目录，不碰用户书库）
# ==========================================================================
def test_library():
    from backend import library, memory

    # 临时库是空的，书库相关的表要自己建
    library.init_db()

    tmp = Path(tempfile.mkdtemp(prefix="evo_lib_"))
    try:
        # 造样本：正常 txt、空文件、损坏 PDF、子目录、被排除目录
        (tmp / "正常书.txt").write_text("第一章 测试\n" + "这是一段用于测试的中文正文内容。" * 40,
                                        encoding="utf-8")
        (tmp / "空文件.txt").write_text("", encoding="utf-8")
        # 扩展名受支持但内容损坏 —— 检验解析器的异常兜底
        (tmp / "损坏.pdf").write_bytes(b"%PDF-1.4\n" + os.urandom(400))
        sub = tmp / "子目录"
        sub.mkdir()
        (sub / "深层.txt").write_text("第二章 深层\n" + "深层目录中的内容。" * 30, encoding="utf-8")
        ex = tmp / "排除我"
        ex.mkdir()
        (ex / "不该收.txt").write_text("排除目录里的内容" * 20, encoding="utf-8")

        holder: dict = {}

        def add_dir():
            r = library.add_dir(str(tmp), "测试库", ["排除我"])
            assert r["ok"], r
            holder["id"] = r["id"]
            return r["id"]

        def add_duplicate_dir():
            r = library.add_dir(str(tmp), "重复", [])
            assert not r["ok"], "重复目录未被拒绝"
            return r["msg"]

        def add_missing_dir():
            r = library.add_dir(str(tmp / "不存在"), "x", [])
            assert not r["ok"]
            return r["msg"]

        def scan_dir():
            r = library.scan([did])
            assert r["ok"]
            assert r["found"] >= 3, f"发现数量异常 {r}"
            return f"发现 {r['found']}，跳过 {r['skipped']}"

        def scan_excludes_work():
            books = library.list_books(dir_id=did, limit=100)["items"]
            paths = [b["path"] for b in books]
            assert not any("排除我" in p for p in paths), "排除规则失效"
            return f"{len(paths)} 本，排除目录未收录"

        def scan_idempotent():
            before = library.library_stats()["books"]
            library.scan([did])
            after = library.library_stats()["books"]
            assert before == after, f"重复扫描产生了重复记录 {before}→{after}"
            return "重复扫描无重复记录"

        def parse_normal():
            b = [x for x in library.list_books(dir_id=did, limit=50)["items"]
                 if x["title"] == "正常书"][0]
            r = library.parse_book(b["id"])
            assert r["ok"], r
            return f"{r['chars']} 字 / {r['chapters']} 章"

        def parse_empty_file():
            b = [x for x in library.list_books(dir_id=did, limit=50)["items"]
                 if x["title"] == "空文件"][0]
            r = library.parse_book(b["id"])
            assert not r["ok"], "空文件竟然解析成功"
            d = library.book_detail(b["id"])
            assert d["status"] == "skipped", f"状态应为 skipped，实际 {d['status']}"
            return f"正确跳过：{d['skip_reason'][:30]}"

        def parse_binary_garbage():
            # 扩展名是 .pdf 但内容是垃圾，应被解析器捕获而不是崩溃
            cands = [x for x in library.list_books(dir_id=did, limit=50)["items"]
                     if x["title"] == "损坏"]
            assert cands, "损坏样本未被扫描登记"
            r = library.parse_book(cands[0]["id"])
            assert not r["ok"], "损坏文件竟然解析成功"
            d = library.book_detail(cands[0]["id"])
            assert d["status"] in ("failed", "skipped"), f"状态异常 {d['status']}"
            return f"正确降级为 {d['status']}"

        def parse_missing_book():
            r = library.parse_book("nonexistent_id_xyz")
            assert not r["ok"]
            return r["msg"]

        def import_book_normal():
            b = [x for x in library.list_books(dir_id=did, limit=50)["items"]
                 if x["title"] == "正常书"][0]
            if FAST:
                return "跳过（--fast）"
            r = library.import_book(b["id"], topic="测试方向", max_chunks=1)
            assert r["ok"], r
            return f"{r['points']} 条知识点"

        def book_detail_missing():
            d = library.book_detail("nope")
            assert d == {}
            return "返回空字典"

        def list_books_paging():
            r1 = library.list_books(limit=1, offset=0)
            r2 = library.list_books(limit=1, offset=1)
            assert len(r1["items"]) <= 1 and len(r2["items"]) <= 1
            return f"总数 {r1['total']}"

        def list_books_bad_limit():
            r = library.list_books(limit=-5)
            assert isinstance(r["items"], list)
            return f"负数 limit 返回 {len(r['items'])} 条"

        def split_chapters_short():
            ch = library._split_chapters("很短的一段话", "标题")
            assert len(ch) == 1
            return "单章兜底"

        def split_chapters_many():
            text = "\n".join(f"第{i}章 标题{i}\n" + "内容" * 120 for i in range(1, 6))
            ch = library._split_chapters(text, "书")
            assert len(ch) >= 4, f"章节切分不足：{len(ch)}"
            return f"{len(ch)} 章"

        def encoding_fallback():
            p = tmp / "gbk书.txt"
            p.write_bytes("这是GBK编码的中文内容测试。".encode("gbk") * 20)
            got = library._read_text_file(p)
            assert "GBK" in got or "编码" in got, f"GBK 解码失败：{got[:30]}"
            return "GBK 正确解码"

        def delete_book():
            b = [x for x in library.list_books(dir_id=did, limit=50)["items"]
                 if x["title"] == "深层"][0]
            library.delete_book(b["id"], purge_points=True)
            assert library.book_detail(b["id"]) == {}
            return "删除成功"

        def recover_interrupted_import():
            # 模拟进程在导入中途被杀：状态卡在 importing
            b = [x for x in library.list_books(dir_id=did, limit=50)["items"]
                 if x["title"] == "正常书"][0]
            c = memory.conn()
            c.execute("UPDATE books SET status='importing' WHERE id=?", (b["id"],))
            c.commit()
            r = library.recover_stale_states()
            assert r["importing"] >= 1, f"没识别出僵尸状态：{r}"
            d = library.book_detail(b["id"])
            # 已经解析出正文的应回到 ready（可重新导入），而不是永远卡住
            assert d["status"] == "ready", f"恢复后状态应为 ready，实际 {d['status']}"
            return f"恢复 {r['importing']} 本 → {d['status']}"

        case("书库", "添加目录", add_dir)
        did = holder.get("id", "")
        case("书库", "重复添加目录被拒", add_duplicate_dir)
        case("书库", "添加不存在目录", add_missing_dir)
        case("书库", "扫描发现文件", scan_dir)
        case("书库", "排除规则生效", scan_excludes_work)
        case("书库", "重复扫描幂等", scan_idempotent)
        case("书库", "解析正常 txt", parse_normal)
        case("书库", "解析空文件", parse_empty_file)
        case("书库", "解析二进制垃圾", parse_binary_garbage)
        case("书库", "解析不存在的书目", parse_missing_book)
        case("书库", "导入并提炼知识点", import_book_normal)
        case("书库", "查询不存在书目", book_detail_missing)
        case("书库", "书目分页", list_books_paging)
        case("书库", "负数 limit 边界", list_books_bad_limit)
        case("书库", "章节切分单章兜底", split_chapters_short)
        case("书库", "章节切分多章", split_chapters_many)
        case("书库", "GBK 编码回退", encoding_fallback)
        case("书库", "删除书目", delete_book)
        case("书库", "导入中断状态恢复", recover_interrupted_import)
    finally:
        # 清掉测试产生的书目记录与目录配置
        try:
            c = memory.conn()
            c.execute("DELETE FROM books WHERE path LIKE ?", (f"{tmp}%",))
            c.execute("DELETE FROM library_dirs WHERE path=?", (str(tmp),))
            c.commit()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# 5. 下载闸门（纯逻辑，不实际下载）
# ==========================================================================
def test_downloader():
    from backend import downloader

    def size_probe():
        g = downloader.repo_size_gb("sentence-transformers/all-MiniLM-L6-v2")
        assert isinstance(g, (int, float))
        return f"{g} GB"

    def size_probe_bogus():
        g = downloader.repo_size_gb("this/repo-does-not-exist-xyz-123")
        assert isinstance(g, (int, float))
        return f"不存在仓库返回 {g}"

    def budget_gate():
        # 只验证闸门逻辑，不触发真实下载（首次会下 0.9GB，不适合放进测试）
        if FAST:
            return "跳过（--fast，避免真实下载）"
        r = downloader.download_hf("sentence-transformers/all-MiniLM-L6-v2",
                                   kind="hf_model", reason="测试")
        assert "ok" in r
        return f"返回 {list(r.keys())}"

    case("下载", "体积探测正常仓库", size_probe)
    case("下载", "体积探测不存在仓库", size_probe_bogus)
    case("下载", "下载闸门返回结构", budget_gate)


# ==========================================================================
# 6. 网络与 LLM（可跳过）
# ==========================================================================
def test_network():
    if FAST:
        _results.append({"group": "网络", "name": "（已跳过，--fast）",
                         "status": WARN, "elapsed": 0, "note": "未执行"})
        return

    from backend import llm, search

    def ollama_health():
        assert llm.healthy(), "Ollama 不可用"
        return "在线"

    def llm_short():
        r = llm.chat([{"role": "user", "content": "只回复两个字：正常"}],
                     temperature=0)
        assert r and len(r) < 200
        return r.strip()[:20]

    def llm_json_mode():
        d = llm.chat_json("你只输出 JSON。",
                          '输出 {"ok": true, "n": 1}', temperature=0)
        assert isinstance(d, dict) and "ok" in d
        return str(d)[:60]

    def search_se():
        r = search.search_stackexchange("retopology quad topology", 4)
        assert isinstance(r, list)
        return f"{len(r)} 条"

    def search_empty_query():
        r = search.search("", limit=3)
        assert isinstance(r, list)
        return f"空查询 {len(r)} 条"

    def search_weird_chars():
        r = search.search("!@#$%^&*()", limit=3)
        assert isinstance(r, list)
        return f"特殊字符 {len(r)} 条"

    case("网络", "Ollama 在线", ollama_health)
    case("网络", "LLM 短回复", llm_short)
    case("网络", "LLM JSON 模式", llm_json_mode)
    case("网络", "StackExchange 检索", search_se)
    case("网络", "空查询检索", search_empty_query)
    case("网络", "特殊字符检索", search_weird_chars)


# ==========================================================================
# 7. 性能基准
# ==========================================================================
def test_perf():
    from backend import library, memory

    def bench_search():
        memory.search("拓扑与细分策略", k=8)
        t0 = time.time()
        for _ in range(10):
            memory.search("拓扑与细分策略", k=8)
        per = (time.time() - t0) / 10
        return f"单次检索 {per*1000:.0f} ms"

    def bench_add_point():
        t0 = time.time()
        n = 10
        for i in range(n):
            memory.add_point(topic="__perf", claim=f"性能测试写入第 {i} 条不同内容用于计时",
                             detail="", source_url="", source_title="",
                             source_kind="web", tags=[], confidence=0.8, session_id="")
        per = (time.time() - t0) / n
        return f"单条写入 {per*1000:.0f} ms"

    def bench_list_books():
        t0 = time.time()
        library.list_books(limit=300)
        return f"300 条书目查询 {(time.time()-t0)*1000:.0f} ms"

    def bench_stats():
        t0 = time.time()
        library.library_stats()
        return f"书库统计 {(time.time()-t0)*1000:.0f} ms"

    case("性能", "语义检索吞吐", bench_search)
    case("性能", "知识点写入", bench_add_point)
    case("性能", "书目列表查询", bench_list_books)
    case("性能", "书库统计", bench_stats)


# ==========================================================================
# 主流程
# ==========================================================================
def main() -> int:
    print("=" * 74)
    print("  EvoMind 测试套件" + ("（快速模式：跳过网络与 LLM）" if FAST else ""))
    print("=" * 74)

    groups = [
        ("配置", test_config),
        ("记忆", test_memory),
        ("检索/OCR 纯函数", test_search_pure),
        ("书库", test_library),
        ("下载", test_downloader),
        ("网络", test_network),
        ("性能", test_perf),
    ]
    for label, fn in groups:
        t0 = time.time()
        try:
            fn()
        except Exception:  # noqa: BLE001
            _results.append({"group": label, "name": "【模块整体异常】", "status": FAIL,
                             "elapsed": time.time() - t0,
                             "note": traceback.format_exc()[-300:]})
        print(f"  已跑完 {label}（{time.time()-t0:.1f}s）")

    print("\n" + "=" * 74)
    print("  结果明细")
    print("=" * 74)
    cur = None
    for r in _results:
        if r["group"] != cur:
            cur = r["group"]
            print(f"\n【{cur}】")
        icon = {"PASS": "✓", "FAIL": "✗", "WARN": "!"}[r["status"]]
        print(f"  {icon} {r['name']:<26} {r['elapsed']*1000:>7.0f}ms  {r['note']}")
        if r.get("trace"):
            print("      " + r["trace"].replace("\n", "\n      ")[-400:])

    p = sum(1 for r in _results if r["status"] == PASS)
    f = sum(1 for r in _results if r["status"] == FAIL)
    w = sum(1 for r in _results if r["status"] == WARN)
    print("\n" + "=" * 74)
    print(f"  通过 {p} · 失败 {f} · 警告 {w} · 共 {len(_results)}")
    print(f"  临时库：{_TMP_DB_DIR}")
    print("=" * 74)
    shutil.rmtree(_TMP_DB_DIR, ignore_errors=True)
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
