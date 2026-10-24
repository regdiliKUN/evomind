"""EvoMind 全局配置。

配置文件位于 data/config.json，可在网页控制台里修改，无需重启进程。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = DATA / "logs"
MODELS = DATA / "models"
DATASETS = DATA / "datasets"
KNOWLEDGE = DATA / "knowledge"
MEDIA = DATA / "media"          # 多模态摄取下载的图片/音频/视频/PDF 缓存
WEB = ROOT / "web"
CONFIG_FILE = DATA / "config.json"
DB_FILE = KNOWLEDGE / "knowledge.db"

for _p in (DATA, LOGS, MODELS, DATASETS, KNOWLEDGE, MEDIA):
    _p.mkdir(parents=True, exist_ok=True)

DEFAULTS: dict[str, Any] = {
    # ---- 推理后端 ----
    "ollama_url": "http://127.0.0.1:11434",
    # 视觉模型：扫描件 OCR 精修用，留空则用默认值 qwen3.8:27b
    "vision_model": "qwen3.8:27b",
    "ocr_dpi": 200,                  # PDF 渲染 DPI，200 够识别正文
    # OCR 跑在哪：auto=有 CUDA 就用 GPU；gpu=强制 GPU；cpu=强制 CPU（排查用）
    "ocr_device": "auto",
    "ocr_workers": 0,                # 并发识别线程数，0=自动（GPU 上 2，纯 CPU 上 1）
    "ocr_rec_batch": 16,             # GPU 上文本行识别的批大小，越大越吃满显卡
    "model": "qwen3.8:27b",          # 主推理模型（大脑）
    "embed_model": "bge-m3",         # 向量化模型（记忆）
    "temperature": 0.3,
    "context_num": 16384,
    # ---- 学习行为 ----
    "max_rounds": 8,                 # 单次学习任务最多迭代轮数
    "queries_per_round": 4,          # 每轮生成多少个检索问题
    "fetch_per_round": 8,            # 每轮最多抓取多少网页
    "min_relevance": 0.45,           # 知识点入库的最低相关度
    "dedup_threshold": 0.93,         # 余弦相似度超过此值视为重复知识点
    "auto_continue": True,           # 学完一轮后是否自动接着深挖
    "idle_minutes": 0.5,             # 兼容旧配置；实际等待不超过30秒
    "idle_seconds": 15,              # 持续自学空闲间隔（1-30秒）
    "learning_seed": "ZBrush",      # 随机学习方向的预设领域
    "continuous_learning": False,    # 持续自学总开关（用户显式开启才生效）
    "auto_rounds": 2,                # 每次自动学习跑几轮
    "last_direction": "",            # 上次学习方向，供持续自学沿用
    # ---- 学习方向自动更换（轮换）----
    # 一个模糊方向 → 拆成若干子方向 → 每个子方向连续学 N 轮 → 自动换下一个。
    "rotation_rounds_per_direction": 100,  # 每个子方向连续学习多少轮
    "rotation_total_rounds": 300,          # 整个轮换任务的总轮次上限
    "rotation_subdirection_count": 6,      # 首次拆出多少个子方向
    "rotation_recent_window": 3,           # 最近多少个方向内不重复选中
    "rotation_boundary_check": True,       # 换方向前校验是否仍属于母方向语义范围
    "rotation_replenish": True,            # 子方向用尽时自动补充新的合法子方向
    "rotation_max_failures": 2,            # 连续多少批没推进就暂停（1-10）
    # ---- 资源自主下载 ----
    "auto_download": True,
    "disk_budget_gb": 200,           # 模型/数据集总占用上限
    "max_model_gb": 40,              # 单个模型体积上限
    "download_workers": 2,
    # ---- 权重层 ----
    # 微调基座。7B 在 4090(24GB) 上实测可跑：LoRA 可训练参数 0.53%，
    # 197 条样本 1 轮 121 秒。想先快速验证链路可改成 Qwen/Qwen2.5-0.5B-Instruct
    "finetune_base": "Qwen/Qwen2.5-7B-Instruct",
    "finetune_min_samples": 300,     # 攒够多少条样本才值得微调
    "finetune_epochs": 2,
    "finetune_lr": 1e-4,
    "finetune_lora_r": 16,
    # ---- 网络 ----
    "request_timeout": 20,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "proxy": "",                     # 可选，如 http://127.0.0.1:7890
    # Brave Search API Key（可选）。国内网络下通用搜索引擎会被反爬污染，
    # 填一个免费 Key（2000 次/月）能显著提升检索质量。
    "search_api_key": "",
    "hf_endpoint": "",               # 可选 HF 镜像，如 https://hf-mirror.com
    # ---- 多模态摄取（图片 / 音频 / 视频 / PDF）----
    # 默认开启：学习时除网页正文外，也会读 PDF、图表示意图、讲座音频与演示视频。
    # 关掉就回到纯文本模式，行为与改造前一致。
    "media_enabled": True,
    # 允许摄取的媒体类型。想只读 PDF 就填 ["pdf"]；填 [] 等于全关。
    "media_kinds": ["pdf", "image", "audio", "video"],
    "media_per_round": 2,            # 每轮学习最多摄取几个媒体来源（控制耗时）
    "media_max_mb": 80,              # 单个媒体文件下载上限（MB）
    "media_timeout": 120,            # 单次媒体下载 / 解码超时（秒）
    "media_keep_files": True,        # 是否把原始媒体留在 data/media 供复查
    # 视觉：把图片交给本地视觉模型（Ollama）描述 + 转录图中文字
    "vision_enabled": True,
    "vision_timeout": 180,           # 单张图视觉推理超时（秒）
    # 语音：faster-whisper 本地转写。模型规格 tiny/base/small/medium/large-v3
    "asr_enabled": True,
    "asr_model": "small",
    "asr_device": "auto",            # auto / cuda / cpu
    "asr_language": "",              # 空=自动检测；中文可填 zh 省一点时间
    "asr_max_seconds": 1800,         # 单个音视频最多转写前 N 秒
    # 视频：优先用字幕（最省算力），无字幕才转写音轨
    "video_subtitles": True,
    "video_frames": 3,               # 无字幕时抽几帧交给视觉模型（0=不抽帧）
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None

#: 改动这些键之后必须重建共享 HTTP 客户端：`httpc` 按 key 缓存客户端，
#: 建好的一刻就把 base_url / 代理 / 默认 UA 固化进去了，改配置不重建等于没生效。
NETWORK_KEYS = frozenset({"ollama_url", "proxy", "user_agent", "request_timeout"})


def _validated(patch: dict[str, Any]) -> dict[str, Any]:
    """忽略未知键，拒绝会破坏后台任务的类型或数值。"""
    ranges = {
        "temperature": (0, 2), "context_num": (256, 131072),
        "ocr_dpi": (72, 600), "max_rounds": (1, 100),
        "ocr_workers": (0, 8), "ocr_rec_batch": (1, 64),
        "queries_per_round": (1, 20), "fetch_per_round": (1, 50),
        "min_relevance": (0, 1), "dedup_threshold": (0, 1),
        "idle_minutes": (0, 10080), "idle_seconds": (1, 30), "auto_rounds": (1, 100),
        "rotation_rounds_per_direction": (1, 2000), "rotation_total_rounds": (1, 100000),
        "rotation_subdirection_count": (2, 20), "rotation_recent_window": (1, 20),
        "rotation_max_failures": (1, 10),
        "disk_budget_gb": (1, 1000000), "max_model_gb": (1, 1000000),
        "download_workers": (1, 16), "finetune_min_samples": (1, 1000000),
        "finetune_epochs": (1, 100), "finetune_lr": (1e-10, 1),
        "finetune_lora_r": (1, 1024), "request_timeout": (1, 600),
        "media_per_round": (0, 20), "media_max_mb": (1, 2048),
        "media_timeout": (5, 1800), "vision_timeout": (10, 1800),
        "asr_max_seconds": (10, 86400), "video_frames": (0, 12),
    }
    clean = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            continue
        expected = type(DEFAULTS[key])
        valid_type = type(value) is expected
        if expected is float:
            valid_type = type(value) in (int, float) and math.isfinite(value)
        if not valid_type:
            raise ValueError(f"{key} 的值类型不正确")
        if key in ranges:
            low, high = ranges[key]
            if not low <= value <= high:
                raise ValueError(f"{key} 必须在 {low} 到 {high} 之间")
        if key in ("model", "embed_model", "ollama_url") and not value.strip():
            raise ValueError(f"{key} 不能为空")
        if key == "media_kinds":
            # 只接受已知类型，去重保序；写错一个字母不该让整条学习管线崩掉
            allowed = ("pdf", "image", "audio", "video")
            clean[key] = [k for k in dict.fromkeys(value) if k in allowed]
            continue
        clean[key] = value
    return clean


def load() -> dict[str, Any]:
    """读取配置，缺失项用默认值补齐并写回。"""
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        cfg = dict(DEFAULTS)
        stored = {}
        if CONFIG_FILE.exists():
            try:
                stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    for key, value in stored.items():
                        try:
                            cfg.update(_validated({key: value}))
                        except ValueError:
                            pass  # 无效旧值只回退对应的默认项
            except (OSError, ValueError):
                pass
        # 环境变量覆盖（便于命令行调试）
        if os.environ.get("EVOMIND_MODEL"):
            cfg["model"] = os.environ["EVOMIND_MODEL"]
        if isinstance(stored, dict) and "idle_seconds" not in stored and "idle_minutes" in stored:
            cfg["idle_seconds"] = max(1, min(30, int(cfg["idle_minutes"] * 60)))
        save(cfg)
        return dict(_cache)


def save(cfg: dict[str, Any]) -> None:
    global _cache
    with _lock:
        clean = {**DEFAULTS, **_validated(cfg)}
        # 同目录原子替换；失败时旧文件与内存缓存都保持不变。
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=CONFIG_FILE.parent,
                prefix=CONFIG_FILE.name + ".", suffix=".tmp", delete=False,
            ) as f:
                temporary = Path(f.name)
                json.dump(clean, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, CONFIG_FILE)
            _cache = dict(clean)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def get(key: str, default: Any = None) -> Any:
    with _lock:
        if _cache is None:
            load()
        return _cache.get(key, default)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        clean = _validated(patch)
        cfg = load()
        cfg.update(clean)
        save(cfg)
        # 只有可能影响共享客户端的键才需要重建。原来无条件 reset()：方向轮换
        # 每换一次方向写一次 last_direction，就把 llm / fetcher / search 的
        # 连接池全部拆掉重来，换向后的第一次检索要重新握手。
        if NETWORK_KEYS & set(clean):
            from . import httpc

            httpc.reset()
        return dict(_cache)


def _disk_usage_gb_uncached() -> float:
    """data/models + data/datasets 当前占用（GB）。"""
    total = 0
    for base in (MODELS, DATASETS):
        for p in base.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return round(total / (1024 ** 3), 2)


#: disk_usage_gb 的 TTL 缓存。仪表盘每 6 秒轮询一次 /api/status，而 memory.stats 与
#: downloader.disk_report 各自都会调 disk_usage_gb —— 不改的话每次轮询要把整棵
#: models/datasets 目录 rglob 两遍（模型文件动辄几万个目录项 + 每文件一次 stat）。
#: TTL 取 15s（>2 个轮询周期）：占用是 GB 级的量，15 秒内的变化肉眼不可见；
#: 下载前的预算闸门走 _disk_usage_gb_uncached()，不受此缓存影响。
_disk_cache: dict[str, Any] = {"ts": 0.0, "value": 0.0}
DISK_USAGE_TTL = 15.0


def disk_usage_gb() -> float:
    """带 5s TTL 的磁盘占用。数值变化以秒计，5s 内的重复轮询看到同一个值没有影响。"""
    now = time.monotonic()
    if now - _disk_cache["ts"] < DISK_USAGE_TTL:
        return _disk_cache["value"]
    val = _disk_usage_gb_uncached()
    _disk_cache.update(ts=now, value=val)
    return val


def free_disk_gb() -> float:
    import shutil

    try:
        return round(shutil.disk_usage(DATA).free / (1024 ** 3), 2)
    except Exception:
        return 0.0
