"""多模态摄取的单元测试。

覆盖三类风险：
  1. 类型判定错位 —— 把网页当视频、把 details 目录页当媒体直链；
  2. 缺依赖时崩溃 —— 视觉模型 / faster-whisper / PyMuPDF 任一缺失，
     必须返回可读的中文原因，而不是抛异常把整轮学习打断；
  3. 管线接线错 —— 媒体候选没有配额限制、图片没有进问答上下文。

全部不依赖网络与真实模型：需要外部能力的地方一律打桩。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config, evolve, fetcher, media, search  # noqa: E402


class KindTests(unittest.TestCase):
    """URL / MIME → 模态。判错的代价是整条管线走错分支。"""

    def test_extension_table(self):
        cases = {
            "https://a.com/a.jpg": "image",
            "https://a.com/a.WEBP": "image",
            "https://a.com/a.mp3": "audio",
            "https://a.com/a.m4a?token=1": "audio",
            "https://a.com/a.mp4": "video",
            "https://a.com/a.pdf": "pdf",
            "https://a.com/post": "web",
            "https://a.com/index.html": "web",
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(media.kind_of(url), want)

    def test_platform_pages_beat_content_type(self):
        # 平台页的 Content-Type 也是 text/html，必须优先按平台判
        self.assertEqual(media.kind_of("https://www.youtube.com/watch?v=x", "text/html"), "video")
        self.assertEqual(media.kind_of("https://youtu.be/x", "text/html"), "video")
        self.assertEqual(media.kind_of("https://www.bilibili.com/video/BV1"), "video")
        self.assertEqual(media.kind_of("https://soundcloud.com/a/b"), "audio")

    def test_archive_details_is_page_but_download_is_file(self):
        self.assertEqual(media.kind_of("https://archive.org/details/lecture-2024"), "video")
        self.assertEqual(media.kind_of("https://archive.org/download/x/a.mp3"), "audio")

    def test_mime_beats_missing_extension(self):
        # CDN 链接没有扩展名，只能靠响应头
        self.assertEqual(media.kind_of("https://cdn.x.com/a1b2c3", "image/png"), "image")
        self.assertEqual(media.kind_of("https://cdn.x.com/a1b2c3", "audio/mpeg; charset=binary"), "audio")
        self.assertEqual(media.kind_of("https://cdn.x.com/a1b2c3", "application/pdf"), "pdf")

    def test_octet_stream_falls_back_to_extension(self):
        self.assertEqual(media.kind_of("https://a.com/a.mp4", "application/octet-stream"), "video")


class SubtitleTests(unittest.TestCase):
    """字幕是视频最省算力的文本来源，解析错了会灌进一堆时间轴噪音。"""

    def test_vtt_strips_timestamps_tags_and_rolls(self):
        vtt = """WEBVTT
Kind: captions
Language: zh

00:00:00.000 --> 00:00:02.000
大家好，今天讲 DynaMesh

00:00:02.000 --> 00:00:04.000
大家好，今天讲 DynaMesh

00:00:04.000 --> 00:00:06.000
<c>分辨率</c>决定面数上限
"""
        text = media._vtt_to_text(vtt)
        self.assertIn("分辨率决定面数上限", text)
        # 自动字幕的滚动重复只保留一次
        self.assertEqual(text.count("今天讲 DynaMesh"), 1)
        self.assertNotIn("-->", text)
        self.assertNotIn("WEBVTT", text)

    def test_srt_timestamps_dropped(self):
        srt = """1
00:00:01,000 --> 00:00:03,000
第一句

2
00:00:03,500 --> 00:00:05,000
第二句
"""
        text = media._vtt_to_text(srt)
        self.assertEqual(text.splitlines(), ["第一句", "第二句"])

    def test_empty_subtitle_returns_empty(self):
        self.assertEqual(media._vtt_to_text("WEBVTT\n\n"), "")

    def test_json3_subtitles_are_parsed_not_passed_through(self):
        """YouTube 同一语言会给 vtt / json3 / ttml 多种格式。
        只按 VTT 解析会把 json3 的原始 JSON 当成正文写进知识库（实测踩过）。"""
        body = ('{"wireMagic":"pb3","events":['
                '{"tStartMs":0,"segs":[{"utf8":"第一句话"}]},'
                '{"tStartMs":1000,"segs":[{"utf8":"第二"},{"utf8":"句话"}]}]}')
        text = media._subs_to_text(body, "json3")
        self.assertEqual(text.splitlines(), ["第一句话", "第二句话"])

    def test_ttml_subtitles_skip_head_metadata(self):
        body = ('<?xml version="1.0"?><tt><head><styling><style id="s1"/></styling></head>'
                '<body><p begin="0s">字幕第一句</p><p begin="1s">字幕第二句</p></body></tt>')
        text = media._subs_to_text(body, "ttml")
        self.assertEqual(text.splitlines(), ["字幕第一句", "字幕第二句"])

    def test_format_is_sniffed_when_ext_missing(self):
        body = '{"events":[{"segs":[{"utf8":"嗅探出来的句子"}]}]}'
        self.assertIn("嗅探出来的句子", media._subs_to_text(body, ""))


class GracefulDegradeTests(unittest.TestCase):
    """缺组件时必须优雅失败：ok=False + 中文原因，绝不抛异常。"""

    def test_image_without_any_engine_reports_reason(self):
        with patch.object(media, "vision_available", return_value=(False, "模型缺失")), \
             patch.object(media, "ocr_image", side_effect=RuntimeError("OCR 组件不可用")):
            got = media.image_text(b"\x89PNG\r\n\x1a\nnot-really-a-png")
        self.assertFalse(got["ok"])
        self.assertTrue(got["error"])
        self.assertIn("OCR", got["error"])

    def test_image_prefers_vision(self):
        with patch.object(media, "vision_available", return_value=(True, "qwen-vl")), \
             patch.object(media, "vision_describe", return_value="【画面】一张图表\n【文字】壁厚 0.8mm"):
            got = media.image_text(b"img")
        self.assertTrue(got["ok"])
        self.assertIn("壁厚", got["text"])
        self.assertTrue(got["engine"].startswith("vision:"))

    def test_image_falls_back_to_ocr_when_vision_raises(self):
        with patch.object(media, "vision_available", return_value=(True, "qwen-vl")), \
             patch.object(media, "vision_describe", side_effect=RuntimeError("boom")), \
             patch.object(media, "ocr_image", return_value="图中文字"):
            got = media.image_text(b"img")
        self.assertTrue(got["ok"])
        self.assertEqual(got["engine"], "rapidocr")
        self.assertIn("图中文字", got["text"])

    def test_broken_pdf_does_not_raise(self):
        got = media.pdf_text(b"not a pdf at all")
        self.assertFalse(got["ok"])
        self.assertTrue(got["error"])

    def test_extract_honours_kind_whitelist(self):
        with patch.object(config, "get", side_effect=lambda k, d=None: (
                ["pdf"] if k == "media_kinds" else True if k == "media_enabled" else d)):
            got = media.extract("https://a.com/a.mp3", kind="audio")
        self.assertFalse(got["ok"])
        self.assertIn("media_kinds", got["error"])

    def test_extract_disabled_returns_reason(self):
        with patch.object(config, "get", side_effect=lambda k, d=None: (
                False if k == "media_enabled" else d)):
            got = media.extract("https://a.com/a.png", kind="image")
        self.assertFalse(got["ok"])
        self.assertIn("关闭", got["error"])


class FetcherDispatchTests(unittest.TestCase):
    """fetcher 必须把媒体 URL 交给 media，而不是报「非文本内容」。"""

    def test_media_url_dispatches_to_media_extract(self):
        with patch.object(media, "extract", return_value={
            "ok": True, "url": "u", "title": "t", "text": "视觉描述", "error": "", "kind": "image",
        }) as fake:
            got = fetcher.fetch("https://a.com/pic.png")
        fake.assert_called_once()
        self.assertEqual(got["kind"], "image")
        self.assertTrue(got["ok"])

    def test_web_url_still_uses_text_path(self):
        # 网页不应误入媒体分支
        with patch.object(media, "extract") as fake:
            try:
                fetcher.fetch("https://example.invalid/post")
            except Exception:  # noqa: BLE001  网络失败可接受，重点是不能调 media
                pass
        fake.assert_not_called()


class CapMediaTests(unittest.TestCase):
    """每轮媒体配额：不设限会让「读 8 篇」变成「转写 8 段音频」。"""

    def test_caps_media_but_keeps_web(self):
        targets = [
            {"kind": "video", "url": "v1"},
            {"kind": "web", "url": "w1"},
            {"kind": "audio", "url": "a1"},
            {"kind": "web", "url": "w2"},
            {"kind": "image", "url": "i1"},
        ]
        kept = evolve.Engine._cap_media(targets, 2)
        kinds = [t["kind"] for t in kept]
        self.assertEqual(kinds.count("video") + kinds.count("audio") + kinds.count("image"), 2)
        self.assertEqual(kinds.count("web"), 2)

    def test_zero_media_limit_drops_all_media(self):
        targets = [{"kind": "pdf", "url": "p"}, {"kind": "web", "url": "w"}]
        kept = evolve.Engine._cap_media(targets, 0)
        self.assertEqual([t["url"] for t in kept], ["w"])

    def test_media_kinds_config_is_sanitised(self):
        clean = config._validated({"media_kinds": ["pdf", "video", "audioo", "pdf"]})
        self.assertEqual(clean["media_kinds"], ["pdf", "video"])


class MediaSearchTests(unittest.TestCase):
    """媒体检索：源能力与模态要对齐，且结果要按 URL 去重。"""

    def test_dedupe_and_kind_routing(self):
        calls: list[tuple[str, str]] = []

        def fake_commons(q, kind="image", limit=6):
            calls.append(("commons", kind))
            return [{"url": "https://x/1.jpg", "title": "a", "kind": kind},
                    {"url": "https://x/2.jpg", "title": "b", "kind": kind}]

        def fake_youtube(q, kind="video", limit=4):
            calls.append(("youtube", kind))
            return [{"url": "https://x/1.jpg", "title": "dup", "kind": kind}]

        with patch.dict(search.MEDIA_SOURCES, {"commons": fake_commons, "youtube": fake_youtube}, clear=True):
            got = search.search_media("dynamesh", kinds=["image", "video"], limit=4)

        # youtube 只应被用于 video，不能被拿来搜图
        self.assertIn(("youtube", "video"), calls)
        self.assertNotIn(("youtube", "image"), calls)
        self.assertEqual(len(got), 2)  # 第三条是重复 URL


class EvidenceTests(unittest.TestCase):
    """证据必须逐字出自正文；媒体来源容错空白，但不放过改写。"""

    TEXT = "【画面】画面展示五只飞鸟，背景为垂直方向的线性渐变天空。\n【文字】无"

    def test_exact_quote_passes(self):
        self.assertTrue(evolve._evidence_supported("画面展示五只飞鸟，背景为垂直方向的线性渐变天空。", self.TEXT))

    def test_whitespace_tolerance(self):
        self.assertTrue(evolve._evidence_supported("画面展示五只飞鸟， 背景为垂直方向的线性渐变天空。", self.TEXT))

    def test_rewrite_is_rejected(self):
        self.assertFalse(evolve._evidence_supported("照片里有五只鸟在天空中飞，天空颜色是渐变的", self.TEXT))

    def test_too_short_is_rejected(self):
        self.assertFalse(evolve._evidence_supported("五只飞鸟", self.TEXT))
        self.assertFalse(evolve._evidence_supported("", self.TEXT))

    def test_media_prompt_differs_from_web_prompt(self):
        # 网页版提示词会把「对画面的描述」判成废话，媒体必须换一套
        self.assertNotEqual(evolve.EXTRACT_MEDIA_SYSTEM, evolve.EXTRACT_SYSTEM)
        self.assertIn("多模态", evolve.EXTRACT_MEDIA_SYSTEM)
        self.assertIn("不要因为", evolve.EXTRACT_MEDIA_SYSTEM)

    def test_pdf_uses_web_prompt(self):
        # PDF 的文字是作者写的、可逐字核对，不该走媒体提示词
        self.assertIn("image", evolve.TRANSCRIBED_KINDS)
        self.assertNotIn("pdf", evolve.TRANSCRIBED_KINDS)


class BackfillTests(unittest.TestCase):
    """查缺补漏：方向清洗、方向选取、忙时拒绝。"""

    def test_book_topic_is_cleaned_for_search(self):
        # 知识库里 95% 是「书库 · 书名 (作者) (Z-Library)」，整串拿去检索是噪音
        cases = {
            "书库 · ZBrush雕塑艺术完美呈现 (邓威, 温洋, 郑淼) (Z-Library)": "ZBrush雕塑艺术完美呈现",
            "书库 · 一个人在阿拉斯加荒野的25年": "一个人在阿拉斯加荒野的25年",
            "ZBrush DynaMesh": "ZBrush DynaMesh",
            "书库 · 怀斯曼强身手册": "怀斯曼强身手册",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw[:24]):
                self.assertEqual(evolve.Engine._query_subject(raw), want)

    def test_subject_never_becomes_empty(self):
        # 全是括号或只有前缀时，必须退回原串而不是空
        for raw in ("书库 · ", "书库 · (作者)", "（作者）"):
            with self.subTest(raw=raw):
                self.assertTrue(evolve.Engine._query_subject(raw).strip())

    def test_explicit_topic_short_circuits(self):
        self.assertEqual(evolve.Engine._backfill_topics("ZBrush DynaMesh", 6), ["ZBrush DynaMesh"])

    def test_busy_engine_refuses_second_backfill(self):
        eng = evolve.Engine()
        eng.state["running"] = True
        try:
            res = eng.backfill_media("x")
        finally:
            eng.state["running"] = False
        self.assertFalse(res["ok"])
        self.assertIn("已有任务", res["msg"])

    def test_empty_knowledge_base_reports_reason(self):
        eng = evolve.Engine()
        with patch.object(evolve.Engine, "_backfill_topics", return_value=[]):
            res = eng.backfill_media("")
        self.assertFalse(res["ok"])
        self.assertIn("知识库", res["msg"])


class AnswerWithImageTests(unittest.TestCase):
    """图片要先转成文字再进问答上下文，纯文本模型也能「看图」。"""

    def test_image_notes_are_injected_into_prompt(self):
        captured = {}

        def fake_chat(messages, **kw):
            captured["user"] = messages[-1]["content"]
            return "基于图片的回答"

        with patch.object(media, "image_text", return_value={
                "ok": True, "text": "【画面】壁厚测量图", "engine": "vision:x", "error": ""}), \
             patch.object(evolve.memory, "search", return_value=[
                 {"claim": "最小壁厚 0.8mm", "detail": "d", "source_title": "t",
                  "source_url": "u", "score": 0.9}]), \
             patch.object(evolve.llm, "chat", side_effect=fake_chat):
            res = evolve.answer_with_knowledge("这张图说明什么", images=["aGVsbG8="])

        self.assertEqual(res["answer"], "基于图片的回答")
        self.assertEqual(res["images"], 1)
        self.assertIn("壁厚测量图", captured["user"])
        self.assertIn("最小壁厚 0.8mm", captured["user"])

    def test_bad_base64_does_not_crash(self):
        with patch.object(evolve.memory, "search", return_value=[
                {"claim": "c", "detail": "d", "source_title": "t", "source_url": "u", "score": 1.0}]), \
             patch.object(evolve.llm, "chat", return_value="ok"):
            res = evolve.answer_with_knowledge("q", images=["!!!not base64!!!"])
        self.assertEqual(res["answer"], "ok")


class CapabilityTests(unittest.TestCase):
    def test_capabilities_shape(self):
        caps = media.capabilities()
        for key in ("vision", "asr", "subtitles", "pdf", "ocr", "frames", "limits", "storage"):
            self.assertIn(key, caps)
        self.assertIn("max_mb", caps["limits"])

    def test_kind_of_never_raises_on_garbage(self):
        for bad in ("", "not a url", "http://[::1", "ftp://x/y.mp3"):
            media.kind_of(bad)  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main(verbosity=2)
