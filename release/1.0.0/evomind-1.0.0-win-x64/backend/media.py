"""多模态摄取：把网上的图片 / 音频 / 视频 / PDF 变成可学习的文本。

改造前 EvoMind 只有一条文本通道：`fetcher.fetch()` 遇到非 text/html 的
Content-Type 直接判失败（"非文本内容"），`_pick_targets()` 还主动把
.pdf/.png/.jpg 从候选里剔掉。于是「网上的图表、讲座录音、演示视频」
全被当成不可读内容丢掉。

这个模块补上三条通道，统一输出 `Doc`（与 fetcher 同构）：

  图片  →  本地视觉模型描述 + 转录图中文字（失败退 RapidOCR）
  音频  →  faster-whisper 本地转写（带时间戳）
  视频  →  平台字幕优先（最省算力）→ 无字幕转写音轨 → 可选抽帧看画面
  PDF   →  PyMuPDF 文本层 → 无文本层（扫描件）逐页 OCR

设计约束（与项目其余部分一致）：
  - **永不抛异常**：任何失败都返回 ok=False + 中文原因，学习循环不会因为
    一个坏链接中断。
  - **依赖全可选**：视觉模型 / faster-whisper / yt-dlp / PyMuPDF 任一缺失
    都只降级对应能力，并在 `capabilities()` 里如实报告，界面据此提示装什么。
  - **有上限**：下载体积、转写时长、抽帧数、文本长度全部受配置约束，
    防止一个 2 小时的视频把一轮学习拖死。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

import httpx

from . import config, httpc

Event = Callable[[str, str, dict], None]
Doc = dict[str, Any]

#: 媒体文本上限：再长对提炼没帮助，只会撑爆上下文
MAX_TEXT = 40000
#: 图片交给视觉模型前的最长边（像素）。原图动辄 4000px、base64 后十几 MB，
#: 既慢又容易被模型内部缩放糟蹋细节。
VISION_MAX_EDGE = 1600
#: 单帧送给视觉模型的提示词
VISION_PROMPT = """你在为知识库做图片信息抽取。严格按下面三段输出，不要客套话、不要总结成一段：

【画面】3-5 句描述主体、场景、对象及其关系。若为图表/示意图，说明坐标轴、图例、单位和趋势；若为界面截图，说明面板、控件与可见数值。

【文字】逐字转录图中所有可读文字，保留标题层级与表格结构（表格用 Markdown）。没有文字就写「无」。

【要点】用不超过 3 条短句列出这张图能支持的事实性结论，必须含具体数字、参数或专有名称；看不出就写「无」。"""


# --------------------------------------------------------------------------
# 类型识别
# --------------------------------------------------------------------------
#: 扩展名 → 模态。查表比猜 Content-Type 可靠：很多静态托管对 mp3 也回
#: application/octet-stream。
EXT_KIND: dict[str, str] = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".gif": "image", ".bmp": "image", ".tif": "image", ".tiff": "image",
    ".avif": "image", ".heic": "image", ".svg": "image",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio",
    ".ogg": "audio", ".oga": "audio", ".opus": "audio", ".aac": "audio",
    ".wma": "audio", ".aiff": "audio",
    ".mp4": "video", ".webm": "video", ".mkv": "video", ".mov": "video",
    ".avi": "video", ".flv": "video", ".m4v": "video", ".wmv": "video",
    ".pdf": "pdf",
}

#: MIME 前缀/精确值 → 模态
MIME_KIND: dict[str, str] = {
    "application/pdf": "pdf",
    "image/svg+xml": "image",
    "audio/mpeg": "audio",
    "audio/mp4": "audio",
    "video/mp4": "video",
    "video/webm": "video",
    "video/quicktime": "video",
}

#: 需要走 yt-dlp 解析的平台页（URL 本身不是媒体文件，页内才有音视频流）
PLATFORM_HOSTS = {
    "youtube.com": "video", "youtu.be": "video", "m.youtube.com": "video",
    "bilibili.com": "video", "b23.tv": "video",
    "vimeo.com": "video", "dailymotion.com": "video", "twitch.tv": "video",
    "soundcloud.com": "audio", "podcasts.apple.com": "audio",
    "ximalaya.com": "audio", "music.163.com": "audio",
    "archive.org": "video",  # details 页，实际类型由 metadata 决定
}

SUPPORTED_KINDS = ("pdf", "image", "audio", "video")


def _ext_of(url: str) -> str:
    try:
        path = urlparse(url).path.lower()
    except ValueError:
        # urlparse 对畸形 URL（如 http://[::1）会抛 ValueError；
        # 这里只做类型判定，畸形链接交给下载阶段去报错。
        return ""
    dot = path.rfind(".")
    if dot < 0 or dot < len(path) - 8:
        return ""
    return path[dot:]


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


def kind_of(url: str, content_type: str = "") -> str:
    """判定一个 URL（可选带响应头）属于哪种模态。

    优先级：平台页 > MIME > 扩展名 > web。
    MIME 放在扩展名之前，是因为 `?file=123` 这类无扩展名链接很常见；
    但平台页必须先判——youtube.com/watch 的 Content-Type 也是 text/html。
    """
    host = _host_of(url)
    for h, kind in PLATFORM_HOSTS.items():
        if host == h or host.endswith("." + h):
            # archive.org 的直链（/download/xxx.mp3）要按扩展名判，只有 details 页才当平台页
            if h == "archive.org" and "/details/" not in url:
                break
            return kind

    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in MIME_KIND:
        return MIME_KIND[ct]
    for prefix in ("image/", "audio/", "video/"):
        if ct.startswith(prefix):
            return prefix[:-1]

    ext_kind = EXT_KIND.get(_ext_of(url))
    if ext_kind:
        return ext_kind
    return "web"


def is_supported(url: str, content_type: str = "") -> bool:
    kind = kind_of(url, content_type)
    return kind in SUPPORTED_KINDS and kind in (config.get("media_kinds") or [])


# --------------------------------------------------------------------------
# HTTP / 落盘
# --------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    return {
        "User-Agent": config.get("user_agent"),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _client(timeout: float | None = None) -> "httpc.Borrowed":
    return httpc.borrow_client(
        "media",
        timeout=timeout or config.get("media_timeout", 120),
        headers=_headers(),
        follow_redirects=True,
    )


def media_dir() -> Path:
    d = Path(config.MEDIA)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guess_ext(content_type: str, kind: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    table = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tiff",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
        "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/flac": ".flac", "video/mp4": ".mp4", "video/webm": ".webm",
        "video/quicktime": ".mov", "video/x-matroska": ".mkv",
        "application/pdf": ".pdf",
    }
    if ct in table:
        return table[ct]
    for prefix, ext in (("image/", ".jpg"), ("audio/", ".mp3"), ("video/", ".mp4")):
        if ct.startswith(prefix):
            return ext
    return {"image": ".jpg", "audio": ".mp3", "video": ".mp4", "pdf": ".pdf"}.get(kind, ".bin")


def _title_from(url: str, headers: dict[str, str]) -> str:
    """从 Content-Disposition 或 URL 末段推标题。"""
    cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    name = ""
    if m:
        name = unquote(m.group(1).strip())
    if not name:
        path = unquote(urlparse(url).path)
        name = path.rsplit("/", 1)[-1]
    name = re.sub(r"\?.*$", "", name)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem.replace("_", " ").replace("-", " ").strip()[:180] or url[:180]


def _archive_details(url: str) -> dict[str, str] | None:
    """archive.org/details/<id> → 真正的媒体直链 + 标题。

    details 页是 HTML 目录页，直接抓只能得到一堆文件名；metadata API
    才拿得到每条的 format 和文件路径。
    """
    m = re.search(r"archive\.org/details/([^/?#]+)", url)
    if not m:
        return None
    ident = m.group(1)
    try:
        with _client(20) as c:
            r = c.get(f"https://archive.org/metadata/{ident}", timeout=20)
            if r.status_code != 200:
                return None
            meta = r.json() or {}
    except Exception:  # noqa: BLE001
        return None
    files = meta.get("files") or []
    mediatype = (meta.get("metadata") or {}).get("mediatype") or ""
    # 优先挑体积适中的原始音视频文件；.torrent/.xml/.sqlite 之类一律跳过
    prefer = ("MPEG4", "h.264", "WebM", "Ogg Vorbis", "MP3", "Flac", "WAVE", "512Kb MPEG4")
    best: tuple[int, str] | None = None
    for f in files:
        name = f.get("name") or ""
        fmt = (f.get("format") or "")
        if not name or name.lower().endswith((".torrent", ".xml", ".sqlite", ".zip", ".gz")):
            continue
        if kind_of(name) not in ("audio", "video"):
            continue
        score = 0
        for i, p in enumerate(prefer):
            if p.lower() in fmt.lower():
                score = len(prefer) - i
                break
        if score and (best is None or score > best[0]):
            best = (score, name)
    if not best:
        return None
    title = (meta.get("metadata") or {}).get("title") or ident
    return {
        "url": f"https://archive.org/download/{quote(ident)}/{quote(best[1])}",
        "title": str(title)[:180],
        "kind": kind_of(best[1]) or ("video" if mediatype == "movies" else "audio"),
    }


def download(url: str, *, kind: str = "", max_mb: float | None = None,
             timeout: float | None = None) -> dict:
    """流式下载到 data/media 缓存。超过体积上限立即中止，不把磁盘写满。

    返回 {ok, path, bytes, content_type, final_url, title, kind, error}
    """
    out: Doc = {
        "ok": False, "path": None, "bytes": 0, "content_type": "",
        "final_url": url, "title": "", "kind": kind, "error": "",
    }
    limit = int((max_mb if max_mb is not None else config.get("media_max_mb", 80)) * 1024 * 1024)
    target = url

    # 本地文件（file://）：复制进缓存，后面所有通道都按同一套流程处理
    if url.startswith("file://"):
        try:
            from urllib.request import url2pathname

            src = Path(url2pathname(unquote(urlparse(url).path)))
            if not src.is_file():
                out["error"] = f"文件不存在：{src}"
                return out
            size = src.stat().st_size
            if size > limit:
                out["error"] = f"文件 {size / 1048576:.0f}MB 超过上限 {limit / 1048576:.0f}MB"
                return out
            digest = hashlib.sha1(str(src).encode("utf-8", "ignore")).hexdigest()[:16]
            dst = media_dir() / f"{digest}{src.suffix.lower()}"
            shutil.copyfile(src, dst)
            out.update(ok=True, path=str(dst), bytes=size, final_url=url,
                       kind=kind or kind_of(src.name), title=src.stem[:180])
            return out
        except Exception as e:  # noqa: BLE001
            out["error"] = f"读取本地文件失败：{str(e)[:120]}"
            return out

    # 平台目录页先解析出真实文件
    if "archive.org/details/" in url:
        got = _archive_details(url)
        if got:
            target = got["url"]
            out["title"] = got["title"]
            kind = kind or got["kind"]

    try:
        with _client(timeout).stream("GET", target, timeout=timeout or config.get("media_timeout", 120)) as r:
            if r.status_code >= 400:
                out["error"] = f"HTTP {r.status_code}"
                return out
            ctype = r.headers.get("content-type", "")
            out["content_type"] = ctype
            out["final_url"] = str(r.url)
            out["kind"] = kind or kind_of(str(r.url), ctype)
            out["title"] = out["title"] or _title_from(str(r.url), dict(r.headers))

            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                out["error"] = f"文件 {int(declared) / 1048576:.0f}MB 超过上限 {limit / 1048576:.0f}MB"
                return out

            digest = hashlib.sha1(target.encode("utf-8", "ignore")).hexdigest()[:16]
            path = media_dir() / f"{digest}{_guess_ext(ctype, out['kind'])}"
            tmp = path.with_suffix(path.suffix + ".part")
            size = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(65536):
                    size += len(chunk)
                    if size > limit:
                        f.close()
                        tmp.unlink(missing_ok=True)
                        out["error"] = f"下载超过上限 {limit / 1048576:.0f}MB，已中止"
                        return out
                    f.write(chunk)
            tmp.replace(path)
            out.update(ok=True, path=str(path), bytes=size)
            return out
    except httpx.HTTPError as e:
        out["error"] = f"下载失败：{type(e).__name__} {str(e)[:120]}"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"下载失败：{str(e)[:160]}"
        return out


# --------------------------------------------------------------------------
# 视觉：图片 → 描述 + 图中文字
# --------------------------------------------------------------------------
_cap_cache: dict[str, Any] = {"ts": 0.0, "value": None}
CAP_TTL = 8.0


def _ollama_base() -> str:
    return (config.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")


def vision_available() -> tuple[bool, str]:
    """本地是否有可用的视觉模型。结果缓存 8 秒，避免每张图都问一次 /api/show。"""
    name = config.get("vision_model") or ""
    if not name:
        return False, "未配置视觉模型（设置 → vision_model）"
    try:
        r = httpx.post(f"{_ollama_base()}/api/show", json={"model": name}, timeout=8)
        if r.status_code != 200:
            return False, f"模型 {name} 不存在（ollama pull {name}）"
        caps = (r.json() or {}).get("capabilities") or []
        if caps and "vision" not in caps:
            return False, f"{name} 不具备视觉能力，请换一个视觉模型"
        return True, name
    except Exception as e:  # noqa: BLE001
        return False, f"Ollama 未响应：{str(e)[:80]}"


def _prepare_image(data: bytes) -> bytes:
    """压到 VISION_MAX_EDGE 以内再送模型。PIL 不在就原样返回。"""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return data
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > VISION_MAX_EDGE:
            img.thumbnail((VISION_MAX_EDGE, VISION_MAX_EDGE))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
        return data
    except Exception:  # noqa: BLE001
        return data


def vision_describe(data: bytes, *, prompt: str = "", timeout: float | None = None) -> str:
    """调本地视觉模型读图。抛异常由调用方兜底。"""
    body = {
        "model": config.get("vision_model"),
        "messages": [{
            "role": "user",
            "content": prompt or VISION_PROMPT,
            "images": [base64.b64encode(_prepare_image(data)).decode()],
        }],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1, "num_predict": 1400},
    }
    r = httpx.post(
        f"{_ollama_base()}/api/chat",
        json=body,
        timeout=timeout or config.get("vision_timeout", 180),
    )
    if r.status_code != 200:
        raise RuntimeError(f"视觉模型 HTTP {r.status_code}")
    msg = (r.json() or {}).get("message") or {}
    return (msg.get("content") or "").strip()


def ocr_image(data: bytes) -> str:
    """RapidOCR 兜底：只认字，不懂画面。"""
    try:
        import numpy as np
        from PIL import Image
        from . import ocr as ocr_mod
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"OCR 组件不可用：{type(e).__name__}") from e
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return ocr_mod.ocr_rapid(np.array(img)).strip()


def image_text(data: bytes, *, on_event: Event | None = None) -> dict:
    """图片 → {ok, text, engine, error}。视觉优先，OCR 兜底，两者都没有就报清楚缺什么。"""
    out = {"ok": False, "text": "", "engine": "", "error": ""}
    if not data:
        out["error"] = "图片为空"
        return out

    if config.get("vision_enabled", True):
        ok, why = vision_available()
        if ok:
            try:
                text = vision_describe(data)
                if text:
                    out.update(ok=True, text=text, engine=f"vision:{why}")
                    return out
                out["error"] = "视觉模型返回空内容"
            except Exception as e:  # noqa: BLE001
                out["error"] = f"视觉模型失败：{str(e)[:120]}"
                if on_event:
                    on_event("warn", f"视觉模型失败，改用 OCR：{str(e)[:80]}", {})
        else:
            out["error"] = why
            if on_event:
                on_event("info", f"视觉模型不可用（{why}），改用 OCR", {})

    try:
        text = ocr_image(data)
        if text:
            out.update(ok=True, text=f"【文字】\n{text}", engine="rapidocr")
            out["error"] = ""
            return out
        out["error"] = out["error"] or "OCR 未识别到文字"
    except Exception as e:  # noqa: BLE001
        # 两条路都断了：把两个原因都留下，用户才知道该装什么
        ocr_err = f"OCR 也失败：{str(e)[:100]}"
        out["error"] = f"{out['error']}；{ocr_err}" if out["error"] else ocr_err
    return out


# --------------------------------------------------------------------------
# 语音：音视频 → 文字
# --------------------------------------------------------------------------
_asr: dict[str, Any] = {"model": None, "key": "", "lock": threading.Lock(), "error": ""}


def asr_available() -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "未安装 faster-whisper（pip install faster-whisper）"
    if not config.get("asr_enabled", True):
        return False, "配置里关闭了语音识别（asr_enabled）"
    return True, str(config.get("asr_model") or "small")


def _patch_av_open() -> None:
    """修 faster-whisper 与新版 PyAV 的签名冲突。

    faster-whisper 1.2 调用 `av.open(..., metadata_errors="ignore")`，
    但 PyAV 15+ 已删掉这个参数；而 Python 3.13 上又装不到旧版 av
    （旧版没有 cp313 wheel，源码编译要整套 FFmpeg 工具链）。
    这里包一层把不认识的参数丢掉——它只影响「容器元数据损坏时是否报错」，
    对正常音视频解码没有影响。
    """
    try:
        import av
    except Exception:  # noqa: BLE001
        return
    if getattr(av, "_evomind_open_patched", False):
        return
    original = av.open

    def open_(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return original(*args, **kwargs)

    av.open = open_
    av._evomind_open_patched = True


def _asr_model():
    """懒加载 WhisperModel。首次调用会下载模型权重（small 约 480MB）。"""
    # huggingface_hub 在 Windows 上默认用符号链接做 blob 去重，某些环境
    # （沙箱、未开开发者模式、跨用户目录）会留下「不受信任的装入点」，
    # 之后加载模型直接报 WinError 448「无法遍历路径」。关掉符号链接，
    # 让它老老实实复制文件，代价是多占一份磁盘。
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    _patch_av_open()

    from faster_whisper import WhisperModel

    size = str(config.get("asr_model") or "small")
    device = str(config.get("asr_device") or "auto")
    if device == "auto":
        device = "cuda"
        try:
            import torch  # noqa: F401

            if not torch.cuda.is_available():
                device = "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    key = f"{size}|{device}|{compute}"
    with _asr["lock"]:
        if _asr["model"] is not None and _asr["key"] == key:
            return _asr["model"]
        model = WhisperModel(size, device=device, compute_type=compute,
                             download_root=str(Path(config.MODELS) / "whisper"))
        _asr.update(model=model, key=key, error="")
        return model


def transcribe(source: Any, *, language: str | None = None) -> dict:
    """转写音频/视频。source 可以是文件路径或文件对象。

    返回 {ok, text, language, duration, segments, error}
    """
    out: Doc = {"ok": False, "text": "", "language": "", "duration": 0.0,
                "segments": [], "error": ""}
    try:
        model = _asr_model()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"语音模型加载失败：{str(e)[:160]}"
        return out

    max_sec = float(config.get("asr_max_seconds", 1800))
    lang = language if language is not None else (config.get("asr_language") or None)
    lines: list[str] = []
    try:
        segments, info = model.transcribe(
            source,
            language=lang or None,
            vad_filter=True,
            beam_size=1,                      # 只做知识抽取，不需要最优解码
            condition_on_previous_text=False,  # 长音频下会累积幻觉
        )
        out["language"] = getattr(info, "language", "") or ""
        out["duration"] = float(getattr(info, "duration", 0) or 0)
        for seg in segments:
            start = float(getattr(seg, "start", 0) or 0)
            if start > max_sec:
                break
            text = (getattr(seg, "text", "") or "").strip()
            if not text:
                continue
            lines.append(f"[{int(start) // 60:02d}:{int(start) % 60:02d}] {text}")
            out["segments"].append({"start": round(start, 1), "text": text})
        text = "\n".join(lines)
        if text:
            out.update(ok=True, text=text)
        else:
            out["error"] = "没有识别到语音内容（可能是纯音乐或静音）"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"转写失败：{str(e)[:160]}"
    return out


# --------------------------------------------------------------------------
# 视频：字幕优先
# --------------------------------------------------------------------------
_SUB_LANGS = ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US", "en-GB")


def _vtt_to_text(vtt: str) -> str:
    """VTT/SRT → 纯文本。自动字幕会滚动重复上一条，这里做去重。"""
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line.isdigit() or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
            continue
        if "-->" in line or re.match(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*$", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = (line.replace("&nbsp;", " ").replace("&amp;", "&")
                    .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if lines and (line == lines[-1] or (lines[-1].endswith(line) and len(line) > 6)):
            continue
        lines.append(line)
    # 合并成自然段：每 12 行断一次，避免整段变成一行
    return "\n".join(lines)


def _json3_to_text(body: str) -> str:
    """YouTube 的 json3 字幕：events[].segs[].utf8 拼成一句。"""
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return ""
    lines: list[str] = []
    for ev in (data.get("events") or []):
        segs = ev.get("segs") or []
        line = "".join(str(s.get("utf8", "")) for s in segs)
        line = re.sub(r"\s+", " ", line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def _xml_subs_to_text(body: str) -> str:
    """ttml / srv3 字幕：只取 <p> / <text> 节点，跳过 head 里的样式定义。"""
    chunks = re.findall(r"<(?:p|text)\b[^>]*>(.*?)</(?:p|text)>", body, re.S)
    if not chunks:
        chunks = [body]
    lines: list[str] = []
    for c in chunks:
        t = re.sub(r"<[^>]+>", "", c)
        t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
              .replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))
        t = re.sub(r"\s+", " ", t).strip()
        if t and (not lines or t != lines[-1]):
            lines.append(t)
    return "\n".join(lines)


def _subs_to_text(body: str, ext: str = "") -> str:
    """按字幕格式分派。YouTube 同一语言会同时给 vtt / json3 / ttml，
    只按 VTT 解析会把 json3 的原始 JSON 当成正文写进知识库。"""
    head = body.lstrip()[:80]
    if ext == "json3" or head.startswith("{"):
        return _json3_to_text(body)
    if ext in ("ttml", "srv3") or head.startswith("<?xml") or "<tt" in head:
        return _xml_subs_to_text(body)
    return _vtt_to_text(body)


def _pick_subtitle(info: dict) -> tuple[str, str]:
    """从 yt-dlp 的 info 里挑一条字幕，返回 (语言, 文本)。人工字幕优先于自动字幕。"""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    # vtt 最好解析；没有 vtt 才退 json3 / ttml，避免拿到无法解析的格式
    for pool, tag in ((manual, "人工字幕"), (auto, "自动字幕")):
        if not pool:
            continue
        langs = [lang for lang in _SUB_LANGS if lang in pool]
        langs += [lang for lang in pool if lang not in langs][:2]
        for lang in langs:
            tracks = pool.get(lang) or []
            for want in ("vtt", "json3", "ttml", "srv3", None):
                for t in tracks:
                    url = t.get("url")
                    ext = t.get("ext")
                    if not url or (want is not None and ext != want):
                        continue
                    if ext not in (None, "vtt", "srv3", "json3", "ttml"):
                        continue
                    try:
                        with _client(30) as c:
                            r = c.get(url, timeout=30)
                            if r.status_code != 200:
                                continue
                            body = r.text
                    except Exception:  # noqa: BLE001
                        continue
                    text = _subs_to_text(body, ext or "")
                    if len(text) > 80:
                        return f"{tag}/{lang}", text
    return "", ""


def _ytdlp_info(url: str) -> dict | None:
    try:
        import yt_dlp
    except Exception:  # noqa: BLE001
        return None
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 25,
        "extractor_args": {"youtube": {"skip": ["hls", "dash"]}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None


def _ytdlp_audio(url: str, *, on_event: Event | None = None) -> str:
    """用 yt-dlp 抓一条纯音频流落盘，返回路径（失败返回空串）。"""
    try:
        import yt_dlp
    except Exception:  # noqa: BLE001
        return ""
    out_dir = media_dir()
    tmpl = str(out_dir / ("ytdl_" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".%(ext)s"))
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "format": "bestaudio[abr<=96]/bestaudio/best",
        "outtmpl": tmpl, "socket_timeout": 30, "retries": 2,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info) if isinstance(info, dict) else ""
        if path and Path(path).exists():
            return path
    except Exception as e:  # noqa: BLE001
        if on_event:
            on_event("warn", f"音轨抓取失败：{str(e)[:100]}", {})
    return ""


def _frames(video_path: str, count: int) -> list[bytes]:
    """抽 N 帧（均匀分布）转 PNG 字节。需要 PyAV；缺失就返回空。"""
    if count <= 0:
        return []
    try:
        import av
        import numpy as np
        import pymupdf
    except Exception:  # noqa: BLE001
        return []
    out: list[bytes] = []
    try:
        with av.open(video_path) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return []
            duration = float(container.duration or 0) / 1_000_000 or 0.0
            if duration <= 0:
                duration = float(getattr(stream, "duration", 0) or 0) * float(stream.time_base or 0.04)
            for i in range(count):
                if duration > 0:
                    try:
                        container.seek(int(duration * (i + 0.5) / count * 1_000_000))
                    except Exception:  # noqa: BLE001
                        pass
                frame = None
                for f in container.decode(stream):
                    frame = f
                    break
                if frame is None:
                    continue
                arr = frame.to_ndarray(format="rgb24")
                h, w = arr.shape[0], arr.shape[1]
                if max(w, h) > VISION_MAX_EDGE:
                    step = max(1, int(max(w, h) / VISION_MAX_EDGE))
                    arr = arr[::step, ::step]
                    h, w = arr.shape[0], arr.shape[1]
                pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(arr).tobytes())
                out.append(pix.tobytes("png"))
    except Exception:  # noqa: BLE001
        return out
    return out


def video_text(url: str, *, on_event: Event | None = None) -> dict:
    """视频 → 文本。顺序：平台字幕 → 音轨转写 → 抽帧看画面。"""
    out: Doc = {"ok": False, "text": "", "engine": "", "error": "", "title": "", "duration": 0.0}
    parts: list[str] = []
    # 平台页必须靠 yt-dlp 解析。缺它时先把话说清楚，别让用户以为是语音识别的锅。
    is_platform = any(_host_of(url) == h or _host_of(url).endswith("." + h) for h in PLATFORM_HOSTS)
    if is_platform:
        try:
            import yt_dlp  # noqa: F401
        except Exception:  # noqa: BLE001
            out["error"] = "未安装 yt-dlp，无法解析 YouTube / Bilibili 等平台视频（pip install yt-dlp）"
            return out
    info = _ytdlp_info(url) if url.startswith("http") else None
    title = ""
    if isinstance(info, dict):
        title = str(info.get("title") or "")
        out["title"] = title[:180]
        out["duration"] = float(info.get("duration") or 0)

    # ① 字幕：最省算力，还能拿到人工校对过的措辞
    if config.get("video_subtitles", True) and isinstance(info, dict):
        lang, sub = _pick_subtitle(info)
        if sub:
            parts.append(f"（视频《{title or url}》字幕 {lang}）\n{sub}")
            out["engine"] = f"subtitle:{lang}"

    # ② 没有字幕就转写音轨
    if not parts:
        ok, why = asr_available()
        if ok:
            if on_event:
                on_event("info", "视频无可用字幕，开始转写音轨", {})
            audio_path = _ytdlp_audio(url, on_event=on_event) if url.startswith("http") else url
            if audio_path:
                tr = transcribe(audio_path)
                if tr["ok"]:
                    parts.append(f"（视频《{title or url}》语音转写）\n{tr['text']}")
                    out["engine"] = f"asr:{why}"
                else:
                    out["error"] = tr["error"]
            else:
                out["error"] = "取不到音轨（平台可能要求登录或已限制下载）"
        else:
            out["error"] = why

    # ③ 抽帧：补画面信息（图表、演示步骤），只在有本地文件时做
    local = url if not url.startswith("http") else ""
    frames = _frames(local, int(config.get("video_frames", 3))) if local else []
    if frames and config.get("vision_enabled", True) and vision_available()[0]:
        shots: list[str] = []
        for i, png in enumerate(frames, 1):
            try:
                desc = vision_describe(png)
                if desc:
                    shots.append(f"· 第 {i} 帧：{desc}")
            except Exception:  # noqa: BLE001
                continue
        if shots:
            parts.append("（视频关键帧）\n" + "\n".join(shots))
            out["engine"] = (out["engine"] + "+frames") if out["engine"] else "frames"

    text = "\n\n".join(p for p in parts if p.strip())
    if text:
        out.update(ok=True, text=text[:MAX_TEXT])
        out["error"] = ""
    elif not out["error"]:
        out["error"] = "视频没有可读内容（无字幕、无音轨，或语音识别未启用）"
    return out


# --------------------------------------------------------------------------
# PDF：文本层优先，扫描件走 OCR
# --------------------------------------------------------------------------
def pdf_text(data: bytes, *, max_pages: int = 60, on_event: Event | None = None) -> dict:
    out: Doc = {"ok": False, "text": "", "engine": "", "error": "", "pages": 0}
    try:
        import pymupdf
    except Exception as e:  # noqa: BLE001
        out["error"] = f"PyMuPDF 不可用：{type(e).__name__}"
        return out
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"PDF 打不开：{str(e)[:120]}"
        return out

    try:
        total = min(len(doc), max_pages)
        out["pages"] = total
        chunks = []
        for i in range(total):
            try:
                t = doc[i].get_text("text").strip()
            except Exception:  # noqa: BLE001
                t = ""
            if t:
                chunks.append(t)
        text = "\n\n".join(chunks)
        # 文本层过少 → 判定为扫描件，逐页渲染后 OCR
        if len(text) < 200 and total:
            if on_event:
                on_event("info", f"PDF 无文本层，改走 OCR（{total} 页）", {})
            try:
                from . import ocr as ocr_mod
                import numpy as np
                pages = []
                for i in range(min(total, 12)):     # OCR 只取前 12 页，控制耗时
                    _png, arr = ocr_mod.render_page(doc, i, dpi=int(config.get("ocr_dpi", 200)))
                    pages.append(ocr_mod.ocr_rapid(arr))
                text = "\n\n".join(p for p in pages if p.strip())
                out["engine"] = "ocr"
            except Exception as e:  # noqa: BLE001
                out["error"] = f"扫描件 OCR 失败：{str(e)[:120]}"
        if text:
            out.update(ok=True, text=text[:MAX_TEXT])
            out["engine"] = out["engine"] or "text-layer"
        else:
            out["error"] = out["error"] or "PDF 里没有可提取的文字"
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    return out


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
def probe(url: str, *, timeout: float = 15) -> dict:
    """只用响应头判断类型，不落盘。

    很多媒体地址没有扩展名（CDN 生成的 /a1b2c3、图片代理、`?id=` 形式），
    光看 URL 只能判成 web。这里用 HEAD 拿 Content-Type；服务器不允许 HEAD
    时退化成 Range: bytes=0-0 的 GET，只取 1 字节。
    """
    out = {"ok": False, "kind": "web", "content_type": "", "size": 0,
           "final_url": url, "error": ""}
    for method, kwargs in (("HEAD", {}), ("GET", {"headers": {"Range": "bytes=0-0"}})):
        try:
            with _client(timeout) as c:
                r = c.request(method, url, timeout=timeout, **kwargs)
                if r.status_code >= 400:
                    out["error"] = f"HTTP {r.status_code}"
                    continue
                ctype = r.headers.get("content-type", "")
                out.update(
                    ok=True,
                    content_type=ctype,
                    kind=kind_of(str(r.url), ctype),
                    size=int(r.headers.get("content-length") or 0),
                    final_url=str(r.url),
                )
                if method == "GET" and r.status_code == 200:
                    # 没走 Range（服务器忽略了头），这里可能已经拉了整个文件，
                    # 立刻关掉连接，不读 body
                    r.close()
                if out["kind"] != "web" or ctype:
                    return out
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {str(e)[:100]}"
            continue
    return out


def extract(url: str, *, kind: str = "", on_event: Event | None = None) -> Doc:
    """把一个媒体 URL 变成可学习文本。

    返回与 `fetcher.fetch` 同构的 Doc：
    {ok, url, title, text, error, kind, meta}
    """
    doc: Doc = {"ok": False, "url": url, "title": "", "text": "",
                "error": "", "kind": kind or kind_of(url), "meta": {}}
    if not config.get("media_enabled", True):
        doc["error"] = "多模态摄取已关闭（设置 → media_enabled）"
        return doc

    # 无扩展名的地址（CDN / 图片代理 / ?id= 形式）先看响应头再定类型
    if doc["kind"] == "web" and url.startswith("http"):
        info = probe(url)
        if info["ok"]:
            doc["kind"] = info["kind"]
            doc["meta"]["content_type"] = info["content_type"]
            if info["size"]:
                doc["meta"]["bytes"] = info["size"]

    if doc["kind"] not in SUPPORTED_KINDS:
        doc["error"] = f"不支持的媒体类型：{doc['kind']}" if doc["kind"] != "web" else \
            "这不是媒体文件（响应头不是图片/音频/视频/PDF），请用「自主学习」按网页抓取"
        return doc
    if doc["kind"] not in (config.get("media_kinds") or []):
        doc["error"] = f"配置未开启 {doc['kind']} 摄取（media_kinds）"
        return doc

    # 平台目录页（archive.org/details）先解析成真实文件直链，避免把 HTML 当媒体
    if "archive.org/details/" in url:
        got = _archive_details(url)
        if got:
            sub = extract(got["url"], kind=got["kind"], on_event=on_event)
            sub["url"] = url                      # 出处仍记目录页，便于回溯
            sub["title"] = sub["title"] or got["title"]
            return sub

    # 视频平台页（URL 本身不是媒体文件）走 yt-dlp，不整段下载
    if doc["kind"] == "video" and url.startswith("http") and _ext_of(url) not in EXT_KIND:
        got = video_text(url, on_event=on_event)
        doc.update(ok=got["ok"], text=got["text"], error=got["error"],
                   title=got.get("title") or url)
        doc["meta"] = {"engine": got.get("engine", ""), "duration": got.get("duration", 0)}
        return doc

    dl = download(url, kind=doc["kind"])
    doc["title"] = dl.get("title") or url
    doc["meta"] = {"bytes": dl.get("bytes", 0), "content_type": dl.get("content_type", "")}
    if not dl["ok"]:
        doc["error"] = dl["error"]
        return doc

    path = Path(dl["path"])
    try:
        if doc["kind"] == "pdf":
            got = pdf_text(path.read_bytes(), on_event=on_event)
            doc.update(ok=got["ok"], text=got["text"], error=got["error"])
            doc["meta"].update(pages=got.get("pages", 0), engine=got.get("engine", ""))
        elif doc["kind"] == "image":
            got = image_text(path.read_bytes(), on_event=on_event)
            doc.update(ok=got["ok"], text=got["text"], error=got["error"])
            doc["meta"]["engine"] = got.get("engine", "")
        elif doc["kind"] == "audio":
            ok, why = asr_available()
            if not ok:
                doc["error"] = why
            else:
                tr = transcribe(str(path))
                doc.update(ok=tr["ok"], text=f"（音频语音转写）\n{tr['text']}" if tr["ok"] else "",
                           error=tr["error"])
                doc["meta"].update(engine=f"asr:{why}", duration=tr.get("duration", 0))
        elif doc["kind"] == "video":
            got = video_text(str(path), on_event=on_event)
            doc.update(ok=got["ok"], text=got["text"], error=got["error"])
            doc["meta"].update(engine=got.get("engine", ""))
    finally:
        if not config.get("media_keep_files", True):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    if doc["ok"]:
        doc["text"] = doc["text"][:MAX_TEXT]
    return doc


# --------------------------------------------------------------------------
# 能力探测（界面用）
# --------------------------------------------------------------------------
def capabilities() -> dict:
    """一次性报告各模态是否可用、缺什么。缓存 8 秒。"""
    now = time.monotonic()
    if _cap_cache["value"] is not None and now - _cap_cache["ts"] < CAP_TTL:
        return dict(_cap_cache["value"])

    vision_ok, vision_why = vision_available()

    asr_ok, asr_why = asr_available()

    try:
        import yt_dlp
        ytdlp = {"ok": True, "version": getattr(yt_dlp.version, "__version__", ""), "reason": ""}
    except Exception:  # noqa: BLE001
        ytdlp = {"ok": False, "version": "", "reason": "未安装 yt-dlp（pip install yt-dlp）"}

    try:
        import pymupdf  # noqa: F401
        pdf = {"ok": True, "reason": ""}
    except Exception:  # noqa: BLE001
        pdf = {"ok": False, "reason": "未安装 PyMuPDF"}

    try:
        # 1.x 叫 rapidocr_onnxruntime（不支持 Python 3.13），2.x 改名为 rapidocr
        import importlib.util

        ocr_ok = any(
            importlib.util.find_spec(m) is not None
            for m in ("rapidocr_onnxruntime", "rapidocr")
        )
        ocr_reason = "" if ocr_ok else "未安装 OCR 引擎（pip install rapidocr）"
    except Exception:  # noqa: BLE001
        ocr_ok, ocr_reason = False, "OCR 组件未安装"

    try:
        import av  # noqa: F401
        av_ok = True
    except Exception:  # noqa: BLE001
        av_ok = False

    value = {
        "enabled": bool(config.get("media_enabled", True)),
        "kinds": list(config.get("media_kinds") or []),
        "vision": {"ok": vision_ok, "model": config.get("vision_model") or "", "reason": vision_why},
        "asr": {"ok": asr_ok, "model": asr_why, "reason": "" if asr_ok else asr_why},
        "subtitles": {"ok": ytdlp["ok"], "reason": ytdlp["reason"]},
        "ytdlp": ytdlp,
        "pdf": pdf,
        "ocr": {"ok": ocr_ok, "reason": ocr_reason},
        "frames": {"ok": av_ok and vision_ok, "reason": "" if av_ok else "未安装 PyAV"},
        "limits": {
            "max_mb": config.get("media_max_mb", 80),
            "per_round": config.get("media_per_round", 2),
            "asr_max_seconds": config.get("asr_max_seconds", 1800),
            "video_frames": config.get("video_frames", 3),
        },
        "storage": stats(),
        "checked_at": time.time(),
    }
    _cap_cache.update(ts=now, value=value)
    return dict(value)


def stats() -> dict:
    """data/media 占用（界面显示用）。"""
    d = Path(config.MEDIA)
    files = 0
    total = 0
    try:
        for p in d.glob("*"):
            try:
                if p.is_file():
                    files += 1
                    total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return {"files": files, "mb": round(total / 1048576, 1), "dir": str(d)}


def clear_cache() -> dict:
    """清空媒体缓存（保留目录本身）。"""
    d = Path(config.MEDIA)
    removed = 0
    if d.exists():
        for p in d.glob("*"):
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    return {"ok": True, "removed": removed, "storage": stats()}
