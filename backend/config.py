"""EvoMind 全局配置。

配置文件位于 data/config.json，可在网页控制台里修改，无需重启进程。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = DATA / "logs"
MODELS = DATA / "models"
DATASETS = DATA / "datasets"
KNOWLEDGE = DATA / "knowledge"
WEB = ROOT / "web"
CONFIG_FILE = DATA / "config.json"
DB_FILE = KNOWLEDGE / "knowledge.db"

for _p in (DATA, LOGS, MODELS, DATASETS, KNOWLEDGE):
    _p.mkdir(parents=True, exist_ok=True)

DEFAULTS: dict[str, Any] = {
    # ---- 推理后端 ----
    "ollama_url": "http://127.0.0.1:11434",
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
    "idle_minutes": 20,              # 空闲多少分钟后自动发起下一轮自学
    "continuous_learning": False,    # 持续自学总开关（用户显式开启才生效）
    "auto_rounds": 2,                # 每次自动学习跑几轮
    "last_direction": "",            # 上次学习方向，供持续自学沿用
    # ---- 资源自主下载 ----
    "auto_download": True,
    "disk_budget_gb": 200,           # 模型/数据集总占用上限
    "max_model_gb": 40,              # 单个模型体积上限
    "download_workers": 2,
    # ---- 权重层 ----
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
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def load() -> dict[str, Any]:
    """读取配置，缺失项用默认值补齐并写回。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        # 环境变量覆盖（便于命令行调试）
        if os.environ.get("EVOMIND_MODEL"):
            cfg["model"] = os.environ["EVOMIND_MODEL"]
        _cache = cfg
        save(cfg)
        return cfg


def save(cfg: dict[str, Any]) -> None:
    global _cache
    with _lock:
        _cache = cfg
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(load())
    cfg.update(patch)
    save(cfg)
    return cfg


def disk_usage_gb() -> float:
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


def free_disk_gb() -> float:
    import shutil

    try:
        return round(shutil.disk_usage(DATA).free / (1024 ** 3), 2)
    except Exception:
        return 0.0
