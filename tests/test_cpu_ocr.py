"""狂暴模式 · CPU 后台 OCR（backend/cpu_ocr.py）端到端测试。

临时库里放一本「扫描版」PDF（页面只有图片、没有文字层），开狂暴模式后
后台线程应把它识别完并转为可导入；关掉后线程应停下。
运行：.venv/Scripts/python -m unittest discover -s tests -p test_cpu_ocr.py
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pymupdf
from test_regressions import IsolatedTest

from backend import config, cpu_ocr, library, memory, ocr, turbo

LINES = ["负形是画面中主体之外的空白区域", "利用负空间让剪影更容易识别", "图底关系要清晰明确"]


def _scanned_pdf(path: Path, pages: int = 3) -> None:
    """先生成带文字的页，再栅格化成纯图片页：得到一本没有文字层的扫描书。"""
    src = pymupdf.open()
    for i in range(pages):
        pg = src.new_page(width=595, height=842)
        # 字要够多：总字数不到 200 会被判成纯图册（与手动整书 OCR 同一口径）
        for j in range(9):
            pg.insert_text((50, 100 + j * 70), f"{LINES[j % 3]}第{i}页{j}行",
                           fontsize=24, fontname="china-s")
    out = pymupdf.open()
    for pg in src:
        pix = pg.get_pixmap(dpi=150)
        np_ = out.new_page(width=595, height=842)
        np_.insert_image(np_.rect, pixmap=pix)
    out.save(path)


class CpuOcrTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        library.init_db()
        ocr.init_db()
        self.pdf = Path(self.tmp.name) / "scan.pdf"
        _scanned_pdf(self.pdf)
        c = memory.conn()
        c.execute(
            "INSERT INTO books(id,path,format,title,pages,status,skip_reason,created_at)"
            " VALUES ('b1',?,'pdf','测试扫描书',3,'skipped','扫描版 PDF（0.0 字/页），无文字层，需 OCR',?)",
            (str(self.pdf), time.time()),
        )
        c.commit()
        self.prio = patch.object(turbo, "_set_priority", return_value=[])
        self.prio.start()
        cpu_ocr._engines.clear()
        self.small = patch.multiple(cpu_ocr, WORKERS=2, THREADS_PER_ENGINE=2, IDLE_RECHECK=0.2)
        self.small.start()

    def tearDown(self):
        turbo.set_enabled(False)
        cpu_ocr.stop(wait=30)
        self.small.stop()
        cpu_ocr._engines.clear()
        self.prio.stop()
        super().tearDown()

    def _wait(self, cond, timeout=120):
        t = time.time()
        while time.time() - t < timeout:
            if cond():
                return True
            time.sleep(0.3)
        return False

    def test_turbo_ocrs_scanned_book_into_ready(self):
        turbo.set_enabled(True)
        c = memory.conn()
        ok = self._wait(lambda: c.execute("SELECT status FROM books WHERE id='b1'").fetchone()[0] == "ready")
        self.assertTrue(ok, f"没有转成可导入：{cpu_ocr.state()}")
        text = "".join(r[0] for r in c.execute("SELECT text FROM ocr_pages WHERE book_id='b1'"))
        self.assertIn("负形", text)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ocr_pages WHERE book_id='b1'").fetchone()[0], 3)
        self.assertGreater(c.execute("SELECT COUNT(*) FROM book_chapters WHERE book_id='b1'").fetchone()[0], 0)
        st = turbo.state()["cpu_ocr"]
        self.assertTrue(st["running"])
        self.assertTrue(self._wait(lambda: cpu_ocr.state()["books_done"] == 1, 10))
        # 识别成果写盘：关掉狂暴模式后还能看到「这次识别了多少」
        turbo.set_enabled(False)
        self.assertTrue(self._wait(lambda: not cpu_ocr.state()["running"], 60))
        last = cpu_ocr.history()["last_run"]
        self.assertEqual((last["books"], last["image_only"], last["pages"]), (1, 0, 3))
        self.assertGreater(last["ended_at"], last["started_at"])
        self.assertEqual(cpu_ocr.state()["totals"]["books"], 1)

    def test_disabled_by_config_or_switch(self):
        config.update({"turbo_cpu_ocr": False})
        turbo.set_enabled(True)
        time.sleep(1)
        self.assertFalse(cpu_ocr.state()["running"])
        config.update({"turbo_cpu_ocr": True})
        turbo.apply()
        self.assertTrue(self._wait(lambda: cpu_ocr.state()["running"], 10))
        turbo.set_enabled(False)
        self.assertTrue(self._wait(lambda: not cpu_ocr.state()["running"], 60))

    def test_image_only_book_is_not_retried_forever(self):
        blank = Path(self.tmp.name) / "blank.pdf"
        d = pymupdf.open()
        d.new_page()
        d.save(blank)
        c = memory.conn()
        c.execute("UPDATE books SET path=? WHERE id='b1'", (str(blank),))
        c.commit()
        turbo.set_enabled(True)
        ok = self._wait(lambda: "需 OCR" not in c.execute(
            "SELECT skip_reason FROM books WHERE id='b1'").fetchone()[0], 60)
        self.assertTrue(ok)
        self.assertEqual(c.execute("SELECT status FROM books WHERE id='b1'").fetchone()[0], "skipped")
        self.assertTrue(self._wait(lambda: (cpu_ocr.history()["last_run"] or {}).get("image_only") == 1, 10))
        self.assertEqual(cpu_ocr.history()["last_run"]["books"], 0)
