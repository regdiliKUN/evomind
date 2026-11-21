"""资源自主下载器：发现并下载模型 / 数据集，带预算与安全阀。

设计原则：
1. 先算体积、再判预算、最后才下 —— 绝不允许把磁盘塞爆。
2. 只下「对当前学习方向有用」的资源，不是见什么下什么。
3. 下载完成的 GGUF 自动注册进 Ollama，直接可用。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import httpx

from . import config, llm, memory, search

Event = Callable[[str, str, dict], None]

_lock = threading.RLock()
_active: set[str] = set()

HF_ENDPOINTS = ["https://huggingface.co", "https://hf-mirror.com"]


def _endpoint() -> str:
    return config.get("hf_endpoint") or "https://huggingface.co"


def _headers() -> dict[str, str]:
    h = {"User-Agent": config.get("user_agent")}
    tok = config.get("hf_token") or os.environ.get("HF_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


# --------------------------------------------------------------------------
# 体积估算
# --------------------------------------------------------------------------
def repo_size_gb(repo: str, kind: str = "model") -> float:
    """通过 HF API 计算仓库总体积（GB）。失败返回 -1。"""
    seg = "models" if kind in ("hf_model", "model") else "datasets"
    for base in [_endpoint()] + HF_ENDPOINTS:
        try:
            with httpx.Client(timeout=20, headers=_headers(), trust_env=False) as c:
                r = c.get(f"{base}/api/{seg}/{repo}", params={"blobs": "true"})
                if r.status_code != 200:
                    continue
                total = 0
                for s in r.json().get("siblings", []):
                    total += s.get("size") or 0
                if total == 0:
                    continue
                return round(total / (1024 ** 3), 2)
        except Exception:
            continue
    return -1.0


def repo_has_gguf(repo: str) -> bool:
    try:
        with httpx.Client(timeout=20, headers=_headers(), trust_env=False) as c:
            r = c.get(f"{_endpoint()}/api/models/{repo}")
            if r.status_code != 200:
                return False
            names = [s.get("rfilename", "") for s in r.json().get("siblings", [])]
            return any(n.lower().endswith(".gguf") for n in names)
    except Exception:
        return False


# --------------------------------------------------------------------------
# 预算闸门
# --------------------------------------------------------------------------
def budget_check(size_gb: float, on_event: Event | None = None) -> tuple[bool, str]:
    if size_gb <= 0:
        return False, "无法确定体积，保守跳过"
    max_one = float(config.get("max_model_gb", 40))
    if size_gb > max_one:
        return False, f"单个体积 {size_gb}GB 超过上限 {max_one}GB"
    # 预算闸门必须用实时值：这里是下载前的最后一道关卡，缓存哪怕 15 秒，
    # 也可能漏掉 concurrently 刚写进去的几个 GB。展示路径才用带 TTL 的版本。
    used = config._disk_usage_gb_uncached()
    budget = float(config.get("disk_budget_gb", 200))
    if used + size_gb > budget:
        return False, f"预算不足：已用 {used}GB / 上限 {budget}GB，本次需 {size_gb}GB"
    free = config.free_disk_gb()
    if free < size_gb * 1.15 + 5:
        return False, f"磁盘剩余 {free}GB 不足以容纳 {size_gb}GB"
    return True, f"体积 {size_gb}GB，预算内（已用 {used}/{budget}GB）"


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def _target_dir(repo: str, kind: str) -> Path:
    base = config.MODELS if kind in ("hf_model", "model", "ollama") else config.DATASETS
    return base / repo.replace("/", "__")


def download_hf(
    repo: str,
    *,
    kind: str = "hf_model",
    reason: str = "",
    direction: str = "",
    on_event: Event | None = None,
) -> dict[str, Any]:
    """下载一个 HF 模型或数据集。返回结果字典。"""
    global _active
    key = f"{kind}:{repo}"
    with _lock:
        if key in _active or memory.resource_exists(repo):
            return {"ok": False, "skipped": True, "msg": "已在队列或已下载"}
        _active.add(key)

    rid = memory.add_resource(
        kind, repo, status="checking", reason=reason, direction=direction
    )

    def emit(level: str, msg: str, payload: dict | None = None) -> None:
        if on_event:
            on_event(level, msg, payload or {})

    try:
        if not config.get("auto_download", True):
            memory.update_resource(rid, status="blocked", reason="自动下载已关闭")
            return {"ok": False, "msg": "自动下载已关闭"}

        size = repo_size_gb(repo, kind)
        ok, why = budget_check(size, on_event)
        if not ok:
            memory.update_resource(rid, status="blocked", size_gb=max(size, 0), reason=why)
            emit("warn", f"跳过 {repo}：{why}", {"repo": repo})
            return {"ok": False, "msg": why}

        memory.update_resource(rid, status="downloading", size_gb=size)
        emit("download", f"开始下载 {repo}（{size}GB）— {reason}", {"repo": repo, "size_gb": size})

        target = _target_dir(repo, kind)
        target.mkdir(parents=True, exist_ok=True)

        from huggingface_hub import snapshot_download

        seg = "models" if kind in ("hf_model", "model") else "datasets"
        last = {"pct": -10}

        def _progress(**kw) -> None:
            total = kw.get("total") or 0
            done = kw.get("downloaded") or 0
            if total:
                pct = int(done * 100 / total)
                if pct >= last["pct"] + 10:
                    last["pct"] = pct
                    emit(
                        "download",
                        f"{repo} 下载中 {pct}%（{done / 1024**3:.2f}/{total / 1024**3:.2f}GB）",
                        {"repo": repo, "pct": pct},
                    )

        local = snapshot_download(
            repo_id=repo,
            repo_type="dataset" if seg == "datasets" else "model",
            local_dir=str(target),
            local_dir_use_symlinks=False,
            max_workers=int(config.get("download_workers", 2)),
            ignore_patterns=["*.md", "*.txt", "*.png", "*.jpg", ".gitattributes"] if seg == "models" else None,
            endpoint=_endpoint(),
            token=config.get("hf_token") or None,
        )

        actual = round(
            sum(f.stat().st_size for f in Path(local).rglob("*") if f.is_file()) / (1024 ** 3), 2
        )
        memory.update_resource(rid, status="done", local_path=str(local), size_gb=actual)
        emit("ok", f"下载完成 {repo}（{actual}GB）→ {local}", {"repo": repo})

        # GGUF 自动注册进 Ollama
        if kind in ("hf_model", "model"):
            ggufs = list(Path(local).rglob("*.gguf"))
            if ggufs:
                name = register_gguf(ggufs[0], repo, emit)
                if name:
                    memory.update_resource(rid, reason=(reason + f" | ollama:{name}").strip(" |"))
        return {"ok": True, "path": str(local), "size_gb": actual}
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        memory.update_resource(rid, status="failed", reason=msg)
        emit("error", f"下载失败 {repo}：{msg}", {"repo": repo})
        return {"ok": False, "msg": msg}
    finally:
        with _lock:
            _active.discard(key)


def register_gguf(gguf_path: Path, repo: str, emit: Event | None = None) -> str | None:
    """把 GGUF 导入 Ollama，命名 evo-<短名>。"""
    exe = shutil.which("ollama") or str(
        Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"
    )
    if not Path(exe).exists():
        return None
    short = re.sub(r"[^a-zA-Z0-9._-]+", "-", repo.split("/")[-1]).lower()[:40]
    name = f"evo-{short}"
    modelfile = gguf_path.parent / "Modelfile.evomind"
    modelfile.write_text(f'FROM "{gguf_path}"\n', encoding="utf-8")
    try:
        subprocess.run(
            [exe, "create", name, "-f", str(modelfile)],
            capture_output=True, timeout=1800, check=True,
        )
        if emit:
            emit("ok", f"已注册进 Ollama：{name}", {"model": name})
        return name
    except Exception:
        return None


def pull_ollama(name: str, *, reason: str = "", direction: str = "", on_event: Event | None = None) -> dict:
    if memory.resource_exists(name):
        return {"ok": False, "msg": "已存在"}
    rid = memory.add_resource("ollama", name, status="downloading", reason=reason, direction=direction)
    try:
        llm.pull_model(name, lambda e: on_event and on_event(
            "download", f"{name}: {e.get('status','')}", {"repo": name}
        ) if e.get("status") else None)
        memory.update_resource(rid, status="done")
        if on_event:
            on_event("ok", f"Ollama 模型就绪：{name}", {"repo": name})
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        memory.update_resource(rid, status="failed", reason=str(e)[:200])
        return {"ok": False, "msg": str(e)[:200]}


# --------------------------------------------------------------------------
# 主动发现
# --------------------------------------------------------------------------
def discover_for_direction(
    direction: str, *, limit: int = 4, on_event: Event | None = None
) -> list[dict]:
    """按学习方向在 HF 上找模型/数据集，判定后自动下载。"""
    if not config.get("auto_download", True):
        return []
    emit = on_event or (lambda *a, **k: None)
    found: list[dict] = []
    try:
        models = search.search_hf(direction, limit=8, kind="model")
        datasets = search.search_hf(direction, limit=5, kind="dataset")
    except Exception:
        return []

    # 用模型自己筛一遍，避免下无用的东西
    cand = [m["title"] for m in models] + [d["title"] for d in datasets]
    cand = [c for c in cand if c and not memory.resource_exists(c)]
    if not cand:
        return []

    picked: list[str] = []
    try:
        verdict = llm.chat_json(
            "你是资源评估器。判断候选资源对某个学习方向是否真的有用。"
            '输出 JSON: {"pick": [{"repo": "...", "type": "hf_model|hf_dataset", "why": "一句话理由", "score": 0-1}]}',
            f"学习方向：{direction}\n候选资源：\n"
            + "\n".join(f"- {c}" for c in cand[:13])
            + "\n\n只挑出 score>=0.6 且确实能帮助该方向学习的，最多 3 个。宁缺毋滥。",
        )
        for item in (verdict.get("pick") or [])[:3]:
            repo = item.get("repo", "")
            if repo and float(item.get("score", 0)) >= 0.6:
                picked.append((repo, item.get("type", "hf_model"), item.get("why", "")))
    except Exception:
        return []

    for repo, rtype, why in picked:
        emit("plan", f"发现可用资源：{repo} — {why}", {"repo": repo})
        r = download_hf(
            repo, kind=rtype, reason=why, direction=direction, on_event=on_event
        )
        found.append({"repo": repo, "why": why, **r})
    return found


def disk_report() -> dict:
    return {
        "used_gb": config.disk_usage_gb(),
        "budget_gb": config.get("disk_budget_gb"),
        "free_gb": config.free_disk_gb(),
        "models": len(list(config.MODELS.glob("*"))) if config.MODELS.exists() else 0,
        "datasets": len(list(config.DATASETS.glob("*"))) if config.DATASETS.exists() else 0,
    }
