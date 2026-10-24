"""扫描件 OCR：把图片型书籍变成可学习的语料。

双引擎设计（实测数据，RTX 4090 / 200 DPI）：
  - rapid：RapidOCR(ONNXRuntime)，CPU 约 2.0 秒/页 → GPU 约 0.15-0.4 秒/页，
            纯文字识别，适合批量跑
  - vision：本地视觉模型（qwen3.8:27b），约 30-60 秒/页，
            能理解表格与版面，质量明显更高，适合精修关键页

调度：渲染（CPU）与识别（GPU）流水线化——一个生产者线程提前把页面渲染进队列，
多个 worker 并行推理，GPU 不会因为等 PDF 渲染而空转。

流程：逐页渲染 → 识别 → 存 ocr_pages → 可校对 → 拼成章节 → 走 library 的提炼入库
支持断点续做：已识别的页会跳过，中断后重跑不会重复消耗时间。
"""
from __future__ import annotations
from . import tasks

import base64
import importlib.util
import io
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pymupdf

from . import config, memory

Event = Callable[[str, str, dict], None]

DEFAULT_DPI = 200          # 200 足够识别正文，再高只是拖慢
MAX_PIXELS = 2600          # 长边上限，防止超大页拖垮识别
VISION_MODEL = "qwen3.8:27b"

_ocr_engine = None
_engine_lock = threading.Lock()

_task: dict[str, Any] = {
    "running": False,
    "book_id": "",
    "book_title": "",
    "engine": "rapid",
    "total": 0,
    "done": 0,
    "failed": 0,
    "page": 0,
    "chars": 0,
    "stage": "idle",
    "stop": False,
    "elapsed": 0.0,
    "log": [],
}
_task_lock = threading.RLock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT,
    page_no INTEGER,
    text TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    chars INTEGER DEFAULT 0,
    elapsed REAL DEFAULT 0,
    edited INTEGER DEFAULT 0,
    created_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_book_page ON ocr_pages(book_id, page_no);
"""


def init_db() -> None:
    c = memory.conn()
    c.executescript(SCHEMA)
    c.commit()


def _emit(on_event: Event | None, level: str, msg: str, **payload) -> None:
    with _task_lock:
        _task["log"].append({"ts": time.time(), "level": level, "msg": msg})
        _task["log"] = _task["log"][-200:]
    if on_event:
        on_event(level, msg, payload)


def state() -> dict:
    with _task_lock:
        d = {k: v for k, v in _task.items() if k != "log"}
        d["log"] = _task["log"][-25:]
        return d


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 设备：让识别真正跑在 GPU 上
# --------------------------------------------------------------------------
_device_info: dict[str, Any] = {"device": "", "providers": []}
_dll_ready = False
_gpu_total: int | None = None


def _torch_lib_dir() -> Path | None:
    """torch 自带的 CUDA/cuDNN DLL 目录（不 import torch，省 1-2 秒启动时间）。"""
    try:
        spec = importlib.util.find_spec("torch")
        if not spec or not spec.submodule_search_locations:
            return None
        lib = Path(list(spec.submodule_search_locations)[0]) / "lib"
        return lib if lib.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


def _enable_cuda_dlls() -> None:
    """让 onnxruntime-gpu 找到 CUDA 运行库。

    ORT 的 CUDA EP 是延迟加载的，按 PATH 找 cublas/cudnn。本机没单独装 CUDA
    Toolkit，但 venv 里的 torch(cu124) 自带 cudart/cublas/cudnn9，把 torch/lib
    加进 DLL 搜索路径就能直接用，省掉几个 G 的独立安装。
    """
    global _dll_ready
    if _dll_ready or os.name != "nt":
        return
    _dll_ready = True
    lib = _torch_lib_dir()
    if not lib:
        return
    try:
        os.add_dll_directory(str(lib))
    except Exception:  # noqa: BLE001
        pass
    os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")


def _cuda_available() -> bool:
    _enable_cuda_dlls()
    try:
        import onnxruntime as ort

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:  # noqa: BLE001
        return False


def _patch_rapidocr_prefix() -> None:
    """修 RapidOCR 1.2.x 的参数前缀 bug。

    它的 UpdateParameters 只给 det 剥了 `det_` 前缀，rec/cls 只认 model_path，
    所以 `rec_use_cuda=True` 会变成一个没人读的键——检测跑在 GPU 上，
    识别却还留在 CPU，整页耗时仍然被识别卡住。这里补上映射。
    """
    from rapidocr_onnxruntime import utils as ru

    if getattr(ru.UpdateParameters, "_evomind_patched", False):
        return

    def _strip(prefix: str, cfg: dict, sub: dict) -> dict:
        if not sub:
            return cfg
        clean: dict[str, Any] = {}
        for k, v in sub.items():
            if k == f"{prefix}use_cuda":
                clean["use_cuda"] = v
            elif k == f"{prefix}model_path":
                clean["model_path"] = v
            elif k == f"{prefix}label_list":
                clean["label_list"] = v
            else:
                # rec_batch_num / cls_batch_num 这类本来就是配置里的键名
                clean[k] = v
        if not clean.get("model_path"):
            clean["model_path"] = str(ru.root_dir / cfg["model_path"])
        cfg.update(clean)
        return cfg

    ru.UpdateParameters.update_cls_params = (
        lambda self, cfg, d: _strip("cls_", cfg, d)
    )
    ru.UpdateParameters.update_rec_params = (
        lambda self, cfg, d: _strip("rec_", cfg, d)
    )
    ru.UpdateParameters._evomind_patched = True


def _rapid_backend() -> str:
    """检测装的是哪一代 RapidOCR。

    1.x 的包名是 rapidocr-onnxruntime，但它的 Requires-Python 卡在 <3.13，
    Python 3.13 的虚拟环境装不上；2.x 改名为 rapidocr 且换了 API。
    两代都要能用，否则「扫描件 OCR」在 3.13 上直接不可用。
    """
    import importlib.util

    for name, tag in (("rapidocr_onnxruntime", "onnxruntime"), ("rapidocr", "rapidocr")):
        try:
            if importlib.util.find_spec(name) is not None:
                return tag
        except Exception:  # noqa: BLE001
            continue
    return ""


class _Rapid2Compat:
    """把 rapidocr 2.x 的返回值适配成 1.x 的 `(items, elapse)`。

    2.x 返回 RapidOCROutput(boxes/txts/scores)，上层 `ocr_rapid()` 是按
    1.x 的 `res, _ = engine(img)` 写的。这里只做形状转换，识别逻辑一行不改。
    """

    def __init__(self, inner, device: str, providers: list[str]) -> None:
        self._inner = inner
        self.device = device
        self.providers = providers

    def __call__(self, img):
        out = self._inner(img)
        # boxes 是 numpy 数组，不能用 `or ()` 兜底（数组真值判断会抛异常）
        boxes = out.boxes if out.boxes is not None else ()
        texts = out.txts if out.txts is not None else ()
        scores = out.scores if out.scores is not None else ()
        items = [(b, t, s) for b, t, s in zip(boxes, texts, scores)]
        return items, float(getattr(out, "elapse", 0.0) or 0.0)


def _new_rapid():
    """构造一个 RapidOCR 实例（不做缓存）。"""
    backend = _rapid_backend()
    if not backend:
        raise RuntimeError(
            "未安装 OCR 引擎。Python 3.13 请装新版：pip install rapidocr"
        )

    want = str(config.get("ocr_device", "auto") or "auto").lower()
    cuda = want != "cpu" and _cuda_available()

    if backend == "rapidocr":
        # 2.x：构造参数是 params 字典，键名带命名空间
        from rapidocr import RapidOCR

        params: dict[str, Any] = {}
        if cuda:
            params["EngineConfig.onnxruntime.use_cuda"] = True
        engine = _Rapid2Compat(RapidOCR(params=params or None),
                               "gpu" if cuda else "cpu",
                               ["CUDAExecutionProvider"] if cuda else ["CPUExecutionProvider"])
        _device_info.update(device=engine.device, providers=engine.providers)
        return engine

    _patch_rapidocr_prefix()
    from rapidocr_onnxruntime import RapidOCR

    kwargs: dict[str, Any] = {
        "det_use_cuda": cuda, "det_model_path": "",
        "cls_use_cuda": cuda, "cls_model_path": "",
        "rec_use_cuda": cuda, "rec_model_path": "",
    }
    if cuda:
        # GPU 上一次多喂几行文本，利用率更高（CPU 上加大反而更慢）
        kwargs["rec_batch_num"] = int(config.get("ocr_rec_batch", 16) or 16)
        kwargs["cls_batch_num"] = 16
    engine = RapidOCR(**kwargs)
    providers: list[str] = []
    try:
        providers = engine.text_detector.infer.session.get_providers()
    except Exception:  # noqa: BLE001
        pass
    _device_info.update(
        device="gpu" if "CUDAExecutionProvider" in providers else "cpu",
        providers=providers,
    )
    return engine


def _rapid():
    global _ocr_engine
    with _engine_lock:
        if _ocr_engine is None:
            _ocr_engine = _new_rapid()
        return _ocr_engine


# 注意：不要给每个 worker 各建一个 RapidOCR 实例。
# 试过（每线程一个 ONNX 会话、各自一条 CUDA 流），单测批跑能从 216 提到 271
# 页/分钟，但放进真实流水线反而更慢：显存被占满后，机器上一并存着的 27B
# 大模型会和 OCR 抢显存，实测从 224 掉到 68-84 页/分钟。单例最稳。


def device_status() -> dict:
    """给前端看：现在识别到底跑在 GPU 还是 CPU，并发开几个。"""
    cuda = _cuda_available()
    want = str(config.get("ocr_device", "auto") or "auto").lower()
    return {
        "cuda_available": cuda,
        "setting": want,
        "active": _device_info.get("device") or ("gpu" if cuda and want != "cpu" else "cpu"),
        "providers": _device_info.get("providers", []),
        "workers": _worker_count("rapid"),
    }


def _gpu_total_mb() -> int:
    """显存总量（MB）。用来决定并发开几个，免得小显存机器 OOM。"""
    global _gpu_total
    if _gpu_total is not None:
        return _gpu_total
    _gpu_total = 0
    try:
        import subprocess

        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            _gpu_total = int(float(out.stdout.strip().splitlines()[0]))
    except Exception:  # noqa: BLE001
        pass
    return _gpu_total


def _worker_count(engine: str) -> int:
    """并发识别线程数。

    vision 引擎走 Ollama，单模型串行，多开会互相排队抢显存，固定 1。
    rapid 在 GPU 上按显存开 2-3 个：GPU 推理时 CPU 在切图/预处理，
    几页交错才能把 GPU 喂满。实测（319 页中文扫描书，41 页样本）：
      CPU 1 线程 24 页/分钟 → GPU 2 线程 124 → 3 线程 170 → 6 线程 183
    6 线程只多 8% 却多吃 3-4GB 显存，不划算。
    纯 CPU 上 ORT 本身已吃满所有核心，开多反而互相拖慢。
    """
    if engine == "vision":
        return 1
    n = int(config.get("ocr_workers", 0) or 0)
    if n > 0:
        return max(1, min(8, n))
    want = str(config.get("ocr_device", "auto") or "auto").lower()
    if want == "cpu" or not _cuda_available():
        return 1
    return 3 if _gpu_total_mb() >= 20000 else 2



def _pix_to_array(pix) -> Any:
    import numpy as np

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    return arr


def render_page(doc, page_no: int, dpi: int = DEFAULT_DPI,
                need_png: bool = True) -> tuple[bytes, Any]:
    """渲染一页为 PNG 字节（need_png 时）+ numpy 数组。

    两处省下的都是纯浪费：

    1. PNG 编码只有视觉引擎用得上（它要把图 base64 发给模型），rapid 引擎
       只吃数组。实测 200DPI 一页编码要 150ms，占单页串行耗时的 9%。
    2. 原来先按原 DPI 渲染一次、再看长边有没有超限，超了就按缩放比例重渲染
       一遍 —— 第一次的产物整个丢掉。A3 及更大的页等于渲染两遍。

    缩放比例用 IRect(page.rect * Matrix) 推出像素尺寸来算，这和 PyMuPDF
    内部算位图尺寸的方式一致，所以一次渲染出来的位图与原来逐字节相同
    （直接拿 page.rect 的磅值算会有亚像素误差，超限页的图会差一点点）。
    """
    page = doc[page_no]
    zoom = dpi / 72.0
    irect = pymupdf.IRect(page.rect * pymupdf.Matrix(zoom, zoom))
    long_edge = max(irect.width, irect.height)
    if long_edge > MAX_PIXELS:
        zoom = zoom * MAX_PIXELS / long_edge
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return (pix.tobytes("png") if need_png else b""), _pix_to_array(pix)


def ocr_rapid(img_array) -> str:
    """RapidOCR：按 y 坐标排序后合并成段落文本。"""
    ocr = _rapid()
    res, _ = ocr(img_array)
    if not res:
        return ""
    items = []
    for box, text, score in res:
        try:
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            items.append((sum(ys) / len(ys), sum(xs) / len(xs), str(text), float(score)))
        except Exception:
            continue
    if not items:
        return ""

    items.sort(key=lambda x: (x[0], x[1]))
    # 同一行（y 接近）的块合并，行间按间距判断是否分段
    lines: list[list[tuple]] = []
    for it in items:
        if lines and abs(it[0] - lines[-1][-1][0]) < 12:
            lines[-1].append(it)
        else:
            lines.append([it])

    out: list[str] = []
    prev_y = None
    for ln in lines:
        ln.sort(key=lambda x: x[1])
        text = "".join(x[2] for x in ln).strip()
        if not text:
            continue
        y = ln[0][0]
        if prev_y is not None and (y - prev_y) > 40:
            out.append("")  # 大间距视为分段
        out.append(text)
        prev_y = y
    return "\n".join(out)


VISION_PROMPT = (
    "识别这张图片中的所有文字，按阅读顺序输出纯文本。\n"
    "要求：\n"
    "- 保留段落与标题层级\n"
    "- 表格用 Markdown 表格输出\n"
    "- 只输出文字本身，不要描述图片、不要加评论\n"
    "- 无法辨认的字符用「□」代替\n"
    "- 图注、页码、章节号也要保留"
)


def ocr_vision(img_bytes: bytes, model: str = "") -> str:
    """用本地视觉模型识别，能理解版面与表格，但慢得多。"""
    b64 = base64.b64encode(img_bytes).decode()
    body = {
        "model": model or config.get("vision_model") or VISION_MODEL,
        "messages": [{"role": "user", "content": VISION_PROMPT, "images": [b64]}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1, "num_predict": 3000},
    }
    r = httpx.post(
        # 配置键是 ollama_url；原来写 ollama_host 永远取不到值，用户改了端口
        # 或换了远程 Ollama，这里仍会去打默认端口。
        f"{(config.get('ollama_url') or 'http://127.0.0.1:11434').rstrip('/')}/api/chat",
        json=body,
        timeout=300,
    )
    if r.status_code != 200:
        raise RuntimeError(f"视觉模型返回 HTTP {r.status_code}")
    return (r.json().get("message", {}) or {}).get("content", "").strip()


def vision_available() -> tuple[bool, str]:
    """检查本地是否有支持视觉的模型。"""
    host = (config.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    name = config.get("vision_model") or VISION_MODEL
    try:
        r = httpx.post(f"{host}/api/show", json={"model": name}, timeout=8)
        if r.status_code != 200:
            return False, f"模型 {name} 不存在"
        caps = (r.json() or {}).get("capabilities") or []
        if "vision" not in caps:
            return False, f"{name} 不支持视觉"
        return True, name
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:80]


# --------------------------------------------------------------------------
# 页范围解析
# --------------------------------------------------------------------------
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn2int(s: str) -> int:
    """把「十二」「二十」「二十三」这类中文数字转成整数。"""
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN_DIGIT.get(a, 1) if a else 1
        ones = _CN_DIGIT.get(b, 0) if b else 0
        return tens * 10 + ones
    return _CN_DIGIT.get(s, 0)


def _normalize_spec(spec: str) -> str:
    """把各种写法统一成「数字-数字,数字」的形式。

    支持：1-50 / 1到50 / 第1页至第50页 / 一-五十 / 1~50 / 1，3，5
    """
    s = (spec or "").strip().lower()
    for a, b in (("到", "-"), ("至", "-"), ("~", "-"), ("～", "-"),
                 ("－", "-"), ("—", "-"), ("–", "-"), ("，", ","), ("、", ","),
                 (" ", ""), ("第", ""), ("页", ""), ("全部", "all"), ("所有", "all")):
        s = s.replace(a, b)
    # 中文数字转阿拉伯数字（逐段替换，只处理 1-99）
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in _CN_DIGIT or ch == "十":
            j = i
            while j < len(s) and (s[j] in _CN_DIGIT or s[j] == "十"):
                j += 1
            out.append(str(_cn2int(s[i:j])))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_pages(spec: str, total: int) -> list[int]:
    """支持 "1-50"、"1,3,5"、"1-20,30-40"、"all"，以及中文写法 "第一到五十页"。页码从 1 开始。"""
    spec = _normalize_spec(spec)
    if spec in ("all", "", "*"):
        return list(range(total))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            a, b = min(a, b), max(a, b)
            pages.update(range(max(1, a) - 1, min(total, b)))
        elif part.isdigit():
            n = int(part) - 1
            if 0 <= n < total:
                pages.add(n)
    return sorted(pages)


def parse_pages_checked(spec: str, total: int) -> tuple[list[int], str]:
    """解析页码并返回错误说明，供 API 层给出明确提示，而不是静默返回空。"""
    normalized = _normalize_spec(spec)
    pages = parse_pages(spec, total)
    if pages:
        return pages, ""
    if normalized in ("all", "", "*"):
        return [], "这本书没有可识别的页面"
    return [], f"看不懂「{spec}」这个页码范围，请用 1-50 或 all 这样的写法"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
@tasks.exclusive("单页OCR")
def ocr_page(book_id: str, page_no: int, engine: str = "rapid", dpi: int = DEFAULT_DPI) -> dict:
    """识别单页并落库。已存在且未编辑的会覆盖。"""
    from . import library

    init_db()
    c = memory.conn()
    row = c.execute("SELECT path, title FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        return {"ok": False, "msg": "书目不存在"}
    doc = pymupdf.open(row["path"])
    if page_no >= doc.page_count:
        return {"ok": False, "msg": "页码超出范围"}

    t0 = time.time()
    img_bytes, arr = render_page(doc, page_no, dpi, need_png=(engine == "vision"))
    if engine == "vision":
        text = ocr_vision(img_bytes)
    else:
        text = ocr_rapid(arr)
    elapsed = time.time() - t0

    c.execute(
        "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
        " VALUES (?,?,?,?,?,?,0,?)"
        " ON CONFLICT(book_id,page_no) DO UPDATE SET"
        " text=excluded.text, engine=excluded.engine, chars=excluded.chars,"
        " elapsed=excluded.elapsed, edited=0",
        (book_id, page_no, text, engine, len(text), elapsed, time.time()),
    )
    c.commit()
    return {"ok": True, "page": page_no, "chars": len(text), "elapsed": round(elapsed, 1), "text": text}


@tasks.exclusive("OCR")
def ocr_book_async(
    book_id: str,
    *,
    pages: str = "all",
    engine: str = "rapid",
    dpi: int = DEFAULT_DPI,
    skip_done: bool = True,
    on_event: Event | None = None,
    _internal: bool = False,
) -> dict:
    """后台批量识别一本书。_internal=True 时跳过互斥检查（供批量队列调用）。"""
    if not _internal:
        with _task_lock:
            if _task["running"] or _task.get("batch"):
                # 看门狗：running 为真但 5 分钟没翻页，说明线程已经死了。
                # 不释放的话，之后每次点「开始识别」都会被互斥检查拒掉，
                # 表现就是「点好几次才动」。
                last = _task.get("last_ts") or 0
                if last and (time.time() - last) > 300:
                    _task.update(running=False, batch=False, stage="idle",
                                 stop=False, last_ts=0)
                else:
                    return {"ok": False, "msg": "已有 OCR 任务在跑，请先等待或停止"}

    # 书库导入和 OCR 都是重任务，同时跑会互相拖慢（都吃 CPU/GPU）
    try:
        from . import finetune as _ft, library as _lib

        if _lib.state().get("running"):
            return {"ok": False, "msg": "书库导入任务正在跑，等它结束再开始 OCR"}
        if _ft.state().get("running"):
            return {"ok": False, "msg": "微调训练正在跑，等它结束再开始 OCR"}
    except Exception:
        pass

    # 磁盘闸门：识别结果要写数据库，空间耗尽会写失败
    try:
        free = config.free_disk_gb()
        if free < 2:
            return {"ok": False, "msg": f"磁盘剩余 {free}GB，空间不足，先清理再 OCR"}
    except Exception:  # noqa: BLE001
        pass

    def go() -> None:
        from . import library

        init_db()
        c = memory.conn()
        row = c.execute("SELECT path, title FROM books WHERE id=?", (book_id,)).fetchone()
        if not row:
            _emit(on_event, "error", "书目不存在")
            with _task_lock:
                _task.update(running=False, stage="idle")
            return
        try:
            doc = pymupdf.open(row["path"])
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "error", f"打不开文件：{str(e)[:80]}")
            with _task_lock:
                _task.update(running=False, stage="idle")
            return

        total_pages = doc.page_count
        targets, page_err = parse_pages_checked(pages, total_pages)
        if page_err:
            _emit(on_event, "error", page_err)
            with _task_lock:
                _task.update(running=False, stage="idle")
            return
        if skip_done:
            done_rows = c.execute(
                "SELECT page_no FROM ocr_pages WHERE book_id=? AND chars>0", (book_id,)
            ).fetchall()
            done = {r["page_no"] for r in done_rows}
            targets = [p for p in targets if p not in done]

        with _task_lock:
            _task.update(
                running=True, book_id=book_id, book_title=row["title"],
                engine=engine, total=len(targets), done=0, failed=0,
                page=0, chars=0, stage="ocr", stop=False, elapsed=0.0,
            )
        _emit(on_event, "stage", f"开始 OCR：《{row['title']}》 共 {len(targets)} 页（引擎 {engine}）")
        if engine != "vision":
            st = device_status()
            _emit(
                on_event, "info",
                f"识别后端：{st['active'].upper()} · 并发 {st['workers']} 线程"
                + ("" if st["cuda_available"] else "（未检测到 CUDA，装 onnxruntime-gpu 可用 GPU）"),
            )

        t_all = time.time()
        workers = _worker_count(engine)
        q: queue.Queue = queue.Queue(maxsize=max(4, workers * 2))
        render_lock = threading.Lock()   # pymupdf 的 doc 不能多线程同时读
        # 只有视觉引擎需要 PNG，rapid 引擎渲染时连编码那一步都省掉
        want_png = engine == "vision"
        counter = {"done": 0}
        stop_flag = threading.Event()

        # 先把模型加载好（1-2 秒），别让它算进第一页的耗时
        if engine != "vision":
            try:
                _rapid()
            except Exception:  # noqa: BLE001
                pass

        def producer() -> None:
            """提前把页面渲染进队列：渲染在 CPU、识别在 GPU，串着做 GPU 会空转。"""
            for pno in targets:
                if stop_flag.is_set() or _task["stop"]:
                    break
                try:
                    with render_lock:
                        img_bytes, arr = render_page(doc, pno, dpi, need_png=want_png)
                    q.put((pno, img_bytes, arr))
                except Exception as e:  # noqa: BLE001
                    _emit(on_event, "warn", f"第 {pno+1} 页渲染失败：{str(e)[:60]}")
                    q.put((pno, b"", None))
            for _ in range(workers):
                q.put(None)

        def worker() -> None:
            wc = memory.conn()   # 每个线程自己的连接（SQLite 连接不能跨线程共享）
            while True:
                item = q.get()
                if item is None:
                    return
                pno, img_bytes, arr = item
                if arr is None:
                    with _task_lock:
                        _task["failed"] += 1
                        counter["done"] += 1
                        _task["done"] = counter["done"]
                        _task["last_ts"] = time.time()
                    continue
                t0 = time.time()
                try:
                    text = ocr_vision(img_bytes) if engine == "vision" else ocr_rapid(arr)
                except Exception as e:  # noqa: BLE001
                    with _task_lock:
                        _task["failed"] += 1
                        counter["done"] += 1
                        _task["done"] = counter["done"]
                        _task["last_ts"] = time.time()
                    _emit(on_event, "warn", f"第 {pno+1} 页识别失败：{str(e)[:70]}")
                    continue
                el = time.time() - t0
                try:
                    wc.execute(
                        "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
                        " VALUES (?,?,?,?,?,?,0,?)"
                        " ON CONFLICT(book_id,page_no) DO UPDATE SET"
                        " text=excluded.text, engine=excluded.engine, chars=excluded.chars,"
                        " elapsed=excluded.elapsed, edited=0",
                        (book_id, pno, text, engine, len(text), el, time.time()),
                    )
                    wc.commit()
                except Exception as e:  # noqa: BLE001
                    _emit(on_event, "warn", f"第 {pno+1} 页写入失败：{str(e)[:60]}")
                with _task_lock:
                    counter["done"] += 1
                    _task["done"] = counter["done"]
                    # 并发下页可能乱序完成，页号取最大值，免得预览往回跳
                    _task["page"] = max(_task["page"], pno + 1)
                    _task["chars"] += len(text)
                    _task["elapsed"] = round(time.time() - t_all, 1)
                    _task["last_ts"] = time.time()
                    n_done = counter["done"]
                if n_done % 5 == 0 or n_done == 1:
                    _emit(
                        on_event, "info",
                        f"第 {pno+1}/{total_pages} 页 · {len(text)} 字 · {el:.1f}s"
                        f"（{n_done}/{len(targets)}）",
                    )

        prod = threading.Thread(target=producer, daemon=True)
        prod.start()
        ws = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for w in ws:
            w.start()
        prod.join()
        for w in ws:
            w.join()
        if _task["stop"]:
            _emit(on_event, "warn", "收到停止指令，已中断（进度已保存，可续做）")

        # 汇总：把有文字的页拼成章节，让书目变成可导入状态
        rows = c.execute(
            "SELECT page_no, text FROM ocr_pages WHERE book_id=? AND chars>0 ORDER BY page_no",
            (book_id,),
        ).fetchall()
        total_chars = sum(len(r["text"]) for r in rows)
        avg = total_chars / max(1, len(rows))
        if total_chars > 200:
            c.execute("DELETE FROM book_chapters WHERE book_id=?", (book_id,))
            # 每 10 页合并为一章，便于分段提炼
            for idx, start in enumerate(range(0, len(rows), 10)):
                group = rows[start:start + 10]
                text = "\n\n".join(g["text"] for g in group)
                title = f"第 {group[0]['page_no']+1}-{group[-1]['page_no']+1} 页"
                c.execute(
                    "INSERT INTO book_chapters(book_id,idx,title,chars) VALUES (?,?,?,?)",
                    (book_id, idx, title, len(text)),
                )
            c.execute(
                "UPDATE books SET status='ready', chars=?, skip_reason='' WHERE id=?",
                (total_chars, book_id),
            )
            msg = f"识别完成：{len(rows)} 页有效，共 {total_chars} 字（平均 {avg:.0f} 字/页），已转为可导入状态"
            _emit(on_event, "ok", msg)
            # 字数明显偏低，说明引擎可能识别不足（日文、表格、复杂版面尤其明显）
            if avg < 150 and engine == "rapid":
                _emit(
                    on_event, "warn",
                    f"平均每页只有 {avg:.0f} 字，识别可能不完整。"
                    "这本书建议改用「精细引擎」重识别，质量会明显更好",
                )
        else:
            _emit(on_event, "warn", f"识别完成但有效文字只有 {total_chars} 字，可能是纯图册")
        c.commit()

        with _task_lock:
            _task.update(running=False, stage="idle")

    # 先占位再起线程：否则批量队列会在线程还没跑到 running=True 时就以为任务结束了，
    # 结果所有书并发开跑，反而更慢
    # 同时把上一本的计数清零：批量换书时旧计数会残留几秒（解析 PDF 期间），
    # 前端就会看到进度条先冲到 100% 再跳回 17%
    with _task_lock:
        _task.update(running=True, stage="starting", book_id=book_id,
                     book_title="", total=0, done=0, failed=0, page=0,
                     chars=0, elapsed=0.0, stop=False, last_ts=time.time())

    # 用 finally 兜底：go() 内部任何未捕获异常都会让 running 永久为 True，
    # 之后所有「开始识别」都会被互斥检查拒掉——表现就是「点好几次才动」
    def _safe_go() -> None:
        try:
            go()
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "error", f"识别异常终止：{str(e)[:100]}")
        finally:
            with _task_lock:
                _task.update(running=False, stage="idle")

    try:
        threading.Thread(target=_safe_go, daemon=True).start()
    except Exception:
        _task.update(running=False, batch=False, stage="idle")
        raise
    return {"ok": True}


@tasks.exclusive("OCR")
def ocr_batch_async(
    book_ids: list[str],
    *,
    pages: str = "all",
    engine: str = "rapid",
    dpi: int = DEFAULT_DPI,
    skip_done: bool = True,
    on_event: Event | None = None,
) -> dict:
    """按顺序批量识别多本书。

    一本跑完再开下一本 —— 同时跑会抢 CPU/显存，反而更慢。
    批量期间 _task["batch"] 为真，单本接口会拒绝新任务。
    """
    ids = [x for x in (book_ids or []) if x]
    if not ids:
        return {"ok": False, "msg": "没有选中要识别的书"}

    with _task_lock:
        if _task["running"] or _task.get("batch"):
            return {"ok": False, "msg": "已有 OCR 任务在跑，请先等待或停止"}

    try:
        from . import finetune as _ft, library as _lib

        if _lib.state().get("running"):
            return {"ok": False, "msg": "书库导入任务正在跑，等它结束再开始 OCR"}
        if _ft.state().get("running"):
            return {"ok": False, "msg": "微调训练正在跑，等它结束再开始 OCR"}
    except Exception:
        pass

    try:
        free = config.free_disk_gb()
        if free < 2:
            return {"ok": False, "msg": f"磁盘剩余 {free}GB，空间不足，先清理再 OCR"}
    except Exception:  # noqa: BLE001
        pass

    with _task_lock:
        _task.update(batch=True, batch_total=len(ids), batch_done=0,
                     running=False, stage="batch", stop=False)

    def go() -> None:
        done = 0
        for i, bid in enumerate(ids, 1):
            if _task.get("stop"):
                break
            _emit(on_event, "stage", f"批量识别 [{i}/{len(ids)}] 本")
            r = ocr_book_async(
                bid, pages=pages, engine=engine, dpi=dpi,
                skip_done=skip_done, on_event=on_event, _internal=True,
            )
            if not r.get("ok"):
                _emit(on_event, "warn", f"第 {i} 本跳过：{str(r.get('msg',''))[:50]}")
                continue
            # 等这一本真正跑完再开下一本
            while _task.get("running"):
                time.sleep(0.8)
                if _task.get("stop"):
                    break
            done = i
            with _task_lock:
                _task["batch_done"] = i
            _emit(on_event, "info", f"批量进度 {i}/{len(ids)}")

        with _task_lock:
            _task.update(batch=False, running=False, stage="idle", stop=False)
        _emit(on_event, "ok", f"批量识别结束：完成 {done}/{len(ids)} 本")

    def _safe_go() -> None:
        try:
            go()
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "error", f"批量识别异常终止：{str(e)[:100]}")
        finally:
            # batch 不清掉的话，之后单本 OCR 会一直被拒
            with _task_lock:
                _task.update(running=False, batch=False, stage="idle", stop=False)

    try:
        threading.Thread(target=_safe_go, daemon=True).start()
    except Exception:
        _task.update(running=False, batch=False, stage="idle")
        raise
    return {"ok": True, "msg": f"已开始批量识别 {len(ids)} 本"}


def stop() -> dict:
    _task["stop"] = True
    return {"ok": True}


# --------------------------------------------------------------------------
# 查询与校对
# --------------------------------------------------------------------------
def list_pages(book_id: str) -> list[dict]:
    init_db()
    rows = memory.conn().execute(
        "SELECT page_no, chars, engine, elapsed, edited FROM ocr_pages"
        " WHERE book_id=? ORDER BY page_no",
        (book_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_page(book_id: str, page_no: int) -> dict:
    init_db()
    row = memory.conn().execute(
        "SELECT * FROM ocr_pages WHERE book_id=? AND page_no=?", (book_id, page_no)
    ).fetchone()
    return dict(row) if row else {}


def save_page(book_id: str, page_no: int, text: str) -> dict:
    """保存人工校对后的文本。"""
    init_db()
    c = memory.conn()
    c.execute(
        "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,elapsed,edited,created_at)"
        " VALUES (?,?,?,?,?,0,1,?)"
        " ON CONFLICT(book_id,page_no) DO UPDATE SET"
        " text=excluded.text, chars=excluded.chars, edited=1",
        (book_id, page_no, text, "manual", len(text), time.time()),
    )
    c.commit()
    return {"ok": True, "chars": len(text)}


def book_text(book_id: str, limit_pages: int = 0) -> str:
    """取出整本书的 OCR 文本，按页序拼接。"""
    init_db()
    sql = "SELECT page_no, text FROM ocr_pages WHERE book_id=? AND chars>0 ORDER BY page_no"
    rows = memory.conn().execute(sql, (book_id,)).fetchall()
    if limit_pages:
        rows = rows[:limit_pages]
    return "\n\n".join(r["text"] for r in rows)


def render_png(book_id: str, page_no: int, dpi: int = 110) -> bytes | None:
    """渲染页面为 PNG，供前端对照校对。"""
    row = memory.conn().execute("SELECT path FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        return None
    try:
        # 显式关闭：前端每翻一页都会调这里，不依赖 GC 回收文件句柄
        with pymupdf.open(row["path"]) as doc:
            if page_no >= doc.page_count:
                return None
            img, _ = render_page(doc, page_no, dpi)
            return img
    except Exception:
        return None


def ocr_stats(book_id: str) -> dict:
    init_db()
    c = memory.conn()
    r = c.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(chars),0) ch, COALESCE(SUM(edited),0) ed"
        " FROM ocr_pages WHERE book_id=?",
        (book_id,),
    ).fetchone()
    return {"pages": r["n"], "chars": r["ch"], "edited": r["ed"]}


@tasks.exclusive("图片OCR")
def ocr_image_bytes(data: bytes, engine: str = "rapid") -> dict:
    """识别用户上传的单张图片（快速体验用）。"""
    import numpy as np

    t0 = time.time()
    if engine == "vision":
        text = ocr_vision(data)
    else:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        text = ocr_rapid(np.array(img))
    return {"ok": True, "text": text, "chars": len(text), "elapsed": round(time.time() - t0, 1)}
