"""权重层进化：把知识库里的积累，用 LoRA 微调固化进模型参数。

与知识层（RAG）的区别：
  - 知识层：查资料，随时可增删，但每次回答都要检索，慢且受上下文限制。
  - 权重层：把知识压进参数，回答变成"本能"，不依赖检索。
两者叠加才是完整的进化。

显存策略：默认 bf16 LoRA + gradient checkpointing（不依赖 bitsandbytes，
Windows 上更稳），7B 模型在 24GB 卡上约 17GB 峰值。
"""
from __future__ import annotations
from . import tasks

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import config, llm, memory

Event = Callable[[str, str, dict], None]
_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "stage": "idle", "progress": 0.0, "log": []}

OUTPUTS = config.DATA / "finetune"


def _emit(on_event: Event | None, level: str, msg: str, **payload) -> None:
    _state["log"].append({"ts": time.time(), "level": level, "msg": msg})
    _state["log"] = _state["log"][-200:]
    if on_event:
        on_event(level, msg, payload)


# --------------------------------------------------------------------------
# 环境检测与安装
# --------------------------------------------------------------------------
ENV_MODULES = ("torch", "transformers", "peft", "datasets")


def _module_versions() -> dict[str, str | None]:
    """只查包元数据、不导入：判断「装没装、什么版本」用不着真的 import。

    原来这里逐个 __import__ torch / transformers / peft / datasets：torch 有一万多个文件、
    4GB+，放在机械盘上冷启动导入要几十秒，而 /api/status 第一次轮询就要等它，
    仪表盘开机后半分钟都是空的；顺带还把 torch 常驻进了服务进程（多占几百 MB 内存）。
    """
    from importlib import metadata, util

    mods: dict[str, str | None] = {}
    for name in ENV_MODULES:
        try:
            if util.find_spec(name) is None:
                mods[name] = None
                continue
            try:
                mods[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                mods[name] = "ok"
        except Exception:  # noqa: BLE001  find_spec 遇到坏包也可能抛
            mods[name] = None
    return mods


#: CUDA 探测在独立子进程里跑：导入 torch 慢且吃内存，不该落在服务进程和请求路径上。
_CUDA_PROBE = (
    "import json, torch\n"
    "ok = torch.cuda.is_available()\n"
    "out = {'cuda': ok, 'gpu': None}\n"
    "if ok:\n"
    "    p = torch.cuda.get_device_properties(0)\n"
    "    out.update(gpu=torch.cuda.get_device_name(0), vram_gb=round(p.total_memory / 1024 ** 3, 1))\n"
    "print(json.dumps(out))\n"
)
CUDA_PROBE_TIMEOUT = 300.0

#: CUDA 探测结果。显卡与驱动不会自己变，探一次即可；装完依赖后由 _reset_env_cache 重探。
_gpu: dict[str, Any] = {"state": "idle", "value": {"cuda": None, "gpu": None}}
_gpu_lock = threading.Lock()


def _probe_cuda() -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run([_python_exe(), "-c", _CUDA_PROBE], capture_output=True, text=True,
                           timeout=CUDA_PROBE_TIMEOUT, creationflags=flags)
        lines = p.stdout.strip().splitlines()
        if p.returncode == 0 and lines:
            return json.loads(lines[-1])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"cuda": False, "gpu": None}


def _run_gpu_probe() -> None:
    value = _probe_cuda()
    with _gpu_lock:
        _gpu.update(state="done", value=value)


def _gpu_info(torch_installed: bool) -> tuple[dict, bool]:
    """返回 (CUDA 信息, 是否仍在探测)。首次调用在后台启动探测，不阻塞。"""
    if not torch_installed:
        return {"cuda": None, "gpu": None}, False
    with _gpu_lock:
        if _gpu["state"] == "idle":
            _gpu["state"] = "running"
            threading.Thread(target=_run_gpu_probe, name="cuda-probe", daemon=True).start()
        return dict(_gpu["value"]), _gpu["state"] == "running"


#: 包元数据查询本身只要几毫秒，TTL 只是避免每次轮询都扫一遍 site-packages。
_env_cache: dict[str, Any] = {"ts": 0.0, "value": None}
CHECK_ENV_TTL = 30.0


def _reset_env_cache() -> None:
    _env_cache.update(ts=0.0, value=None)
    with _gpu_lock:
        if _gpu["state"] != "running":
            _gpu.update(state="idle", value={"cuda": None, "gpu": None})


def check_env() -> dict:
    """微调环境检测，永不阻塞：CUDA 信息未探测完时 cuda=None、cuda_checking=True。"""
    now = time.monotonic()
    if _env_cache["value"] is None or now - _env_cache["ts"] >= CHECK_ENV_TTL:
        _env_cache.update(ts=now, value=_module_versions())
    mods = dict(_env_cache["value"])
    gpu, checking = _gpu_info(bool(mods.get("torch")))
    ready = all(v for k, v in mods.items() if k != "datasets")
    return {"ready": ready, "modules": mods, **gpu, "cuda_checking": checking}


def _python_exe() -> str:
    """子进程用当前解释器；打包成 exe 时退回项目自带 venv。"""
    if getattr(sys, "frozen", False):
        return str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe")
    return sys.executable


INSTALL_CMD = (
    "pip install 'torch>=2.4,<3.0' --index-url https://download.pytorch.org/whl/cu124 && "
    "pip install 'transformers>=4.40,<6.0' 'peft>=0.10,<1.0' "
    "'datasets>=2.0,<4.0' 'accelerate>=0.30,<2.0' "
    "'sentencepiece>=0.2,<1.0' 'protobuf>=4.21,<8.0' 'safetensors>=0.4,<1.0'"
)


def install_env(on_event: Event | None = None) -> dict:
    """安装微调依赖（体积较大，约 3GB）。"""
    py = _python_exe()
    steps = [
        ["-m", "pip", "install", "torch>=2.4,<3.0", "--index-url",
         "https://download.pytorch.org/whl/cu124"],
        ["-m", "pip", "install", "transformers>=4.40,<6.0", "peft>=0.10,<1.0",
         "datasets>=2.0,<4.0", "accelerate>=0.30,<2.0",
         "sentencepiece>=0.2,<1.0", "protobuf>=4.21,<8.0", "safetensors>=0.4,<1.0"],
    ]
    for cmd in steps:
        _emit(on_event, "info", f"安装：{' '.join(cmd[2:])}")
        try:
            p = subprocess.run([py] + cmd, capture_output=True, text=True, timeout=3600)
            if p.returncode != 0:
                _emit(on_event, "error", f"安装失败：{p.stderr[-400:]}")
                return {"ok": False, "msg": p.stderr[-400:]}
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "error", f"安装异常：{e}")
            return {"ok": False, "msg": str(e)}
    # 刚装完依赖，环境检测缓存必须失效，否则界面要等最多 30s 才反映新状态
    _reset_env_cache()
    ok = check_env()["ready"]
    _emit(on_event, "ok" if ok else "warn", "微调环境就绪" if ok else "依赖仍不完整")
    return {"ok": ok}


# --------------------------------------------------------------------------
# 数据集构建
# --------------------------------------------------------------------------
DATASET_SYSTEM = "你是一个在特定领域经过深度学习的助手。请准确、具体地回答问题。"

REWRITE_SYSTEM = """你是训练数据构造器。为每条知识陈述生成一个人类会真实提出的问题。

要求：
- 问题必须能被对应的陈述直接回答，不要问陈述里没有的信息
- 不要出现「这个要点」「上述内容」「该领域」这类指代
- 不要提及书名、章节、来源
- 用自然口语，像真人提问，长度 10-40 字
- 只输出 JSON，格式：{"questions": ["问题1", "问题2", ...]}，顺序与输入一致"""


def build_dataset(
    topic: str | None = None,
    *,
    min_samples: int | None = None,
    rewrite: bool = True,
    rewrite_limit: int | None = None,
    on_event: Event | None = None,
) -> dict:
    """把知识点转成训练样本。

    先用模型把模板问句改写成自然提问（默认开启）——模板问句训出来的模型
    只会复读，不会回答问题。样本仍不足时再扩写补量。

    改写很慢（27B 模型每批 8 条约 30-60 秒），rewrite_limit 可用于快速验证。
    """
    min_samples = int(min_samples or config.get("finetune_min_samples", 300))
    _state.update(stage="导出知识库", progress=0.02)
    pairs = memory.export_training_pairs(topic, limit=8000)
    _emit(on_event, "info", f"知识库导出 {len(pairs)} 条原始样本")
    if not pairs:
        _state.update(stage="知识库为空", progress=0.0)
        return {"ok": False, "msg": "知识库为空，先去学习一轮"}

    # ---- 1. 改写问句（质量优先）----
    if rewrite:
        target = int(rewrite_limit) if rewrite_limit else min(len(pairs), max(min_samples, 120))
        target = min(target, len(pairs))

        # 断点续做：每条要 5-6 秒，200 条要 20 分钟，中途关窗口不该从头再来
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        safe_tag = (topic or "all").replace("/", "_").replace("\\", "_")[:30]
        partial = OUTPUTS / f"rewrite_{safe_tag}.partial.json"
        saved: list[str] = [""] * len(pairs)
        if partial.exists():
            try:
                prev = json.loads(partial.read_text(encoding="utf-8"))
                if isinstance(prev, list) and len(prev) == len(pairs):
                    saved = [str(x or "") for x in prev]
                    restored = sum(1 for q in saved if q)
                    if restored:
                        for i, q in enumerate(saved):
                            if q:
                                pairs[i]["instruction"] = q
                        _emit(on_event, "info", f"发现上次进度，已恢复 {restored} 条改写结果")
            except Exception:  # noqa: BLE001
                saved = [""] * len(pairs)

        _emit(on_event, "info", f"用模型改写 {target} 条问句，让问题和答案真正对得上…")
        batch = 8
        done = 0
        for i in range(0, target, batch):
            chunk = pairs[i : i + batch]
            # 只改写这一批里还没改过的条目，已改的原样保留
            todo = [(j, p) for j, p in enumerate(chunk) if not saved[i + j]]
            if not todo:
                done += len(chunk)
                continue
            payload = "\n".join(
                f"{k+1}. {p['output'][:180]}" for k, (_, p) in enumerate(todo)
            )
            try:
                data = llm.chat_json(
                    REWRITE_SYSTEM, f"知识陈述：\n{payload}", temperature=0.6
                )
                qs = data.get("questions") or []
                for k, q in enumerate(qs):
                    if k < len(todo) and isinstance(q, str) and len(q.strip()) > 8:
                        j = todo[k][0]
                        chunk[j]["instruction"] = q.strip()
                        saved[i + j] = q.strip()
                done += len(chunk)
            except Exception as e:  # noqa: BLE001
                _emit(on_event, "warn", f"第 {i//batch+1} 批改写失败（{str(e)[:40]}），保留模板问句")
                continue
            # 每 5 批落盘一次，中断最多丢 5 批
            if (i // batch + 1) % 5 == 0:
                try:
                    partial.write_text(
                        json.dumps(saved, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception:  # noqa: BLE001
                    pass
                # 改写占整体进度的大头：0.05 → 0.90
                _state.update(
                    progress=round(0.05 + 0.85 * min(1.0, done / max(target, 1)), 3),
                    stage=f"改写问句 {done}/{target}",
                )
                _emit(on_event, "info", f"已改写 {done}/{target} 条")
        try:
            partial.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        _emit(on_event, "ok", f"问句改写完成：{done} 条")

    # ---- 2. 样本不足时扩写补量 ----
    if len(pairs) < min_samples:
        _emit(on_event, "info", "样本数量不足，用模型扩写中…")
        expanded: list[dict] = []
        ebatch = 6
        for i in range(0, min(len(pairs), 120), ebatch):
            chunk = pairs[i : i + ebatch]
            payload = "\n".join(f"{j+1}. {p['output'][:200]}" for j, p in enumerate(chunk))
            try:
                data = llm.chat_json(
                    "你是训练数据构造器。把每条知识陈述改写成 2 个不同角度的问答对，"
                    '输出 JSON: {"pairs":[{"q":"...","a":"..."}]}。答案要基于原文，不得编造。',
                    f"知识点：\n{payload}",
                    temperature=0.7,
                )
                for qa in data.get("pairs") or []:
                    q, a = (qa.get("q") or "").strip(), (qa.get("a") or "").strip()
                    if len(q) > 6 and len(a) > 12:
                        expanded.append({"instruction": q, "output": a, "source": "expanded"})
            except Exception as e:  # noqa: BLE001
                _emit(on_event, "warn", f"扩写批次失败：{str(e)[:40]}")
                continue
            if len(expanded) + len(pairs) >= min_samples:
                break
        pairs.extend(expanded)
        _emit(on_event, "info", f"扩写后共 {len(pairs)} 条样本")

    OUTPUTS.mkdir(parents=True, exist_ok=True)
    tag = (topic or "all").replace("/", "_")[:40]
    path = OUTPUTS / f"dataset_{tag}_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(
                {
                    "messages": [
                        {"role": "system", "content": DATASET_SYSTEM},
                        {"role": "user", "content": p["instruction"]},
                        {"role": "assistant", "content": p["output"]},
                    ],
                    # 行级溯源：纠错 / 归档之后能反查「权重里这一条来自哪个知识点、
                    # 哪个 revision」。扩写样本没有原始点，也显式标注来源，不留空档。
                    "provenance": p.get("provenance") or {
                        "origin": "expanded", "point_id": "", "revision": 0,
                    },
                },
                ensure_ascii=False,
            ) + "\n")
    _state.update(stage=f"完成：{len(pairs)} 条", progress=1.0)
    _emit(on_event, "ok", f"数据集已生成：{path.name}（{len(pairs)} 条）")
    return {"ok": True, "path": str(path), "count": len(pairs)}


@tasks.exclusive("微调任务")
def build_dataset_async(
    topic: str | None = None,
    *,
    min_samples: int | None = None,
    rewrite: bool = True,
    on_event: Event | None = None,
) -> dict:
    """后台生成训练集。

    改写几百条要十几分钟，同步接口会让请求一直挂着、前端只能干等，
    所以改成后台跑 + 前端轮询 /api/finetune/state 看进度。
    """
    with _lock:
        if _state["running"]:
            return {"ok": False, "msg": "已有任务在跑"}
        _state.update(running=True, stage="准备", progress=0.0, log=[], result=None)

    def go() -> None:
        try:
            r = build_dataset(
                topic, min_samples=min_samples, rewrite=rewrite, on_event=on_event
            )
            _state["result"] = r
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "error", f"生成训练集失败：{str(e)[:120]}")
            _state["result"] = {"ok": False, "msg": str(e)}
        finally:
            with _lock:
                _state["running"] = False

    try:
        threading.Thread(target=go, daemon=True).start()
    except Exception:
        _state["running"] = False
        raise
    return {"ok": True, "msg": "已开始生成训练集"}


# --------------------------------------------------------------------------
# 训练
# --------------------------------------------------------------------------
def _format_example(tok, ex: dict) -> str:
    msgs = ex["messages"]
    try:
        return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
    except Exception:
        parts = [f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>" for m in msgs]
        return "\n".join(parts)


def train(
    dataset_path: str,
    *,
    base: str | None = None,
    epochs: int | None = None,
    lr: float | None = None,
    on_event: Event | None = None,
    _reserved: bool = False,
) -> dict:
    global _state
    with _lock:
        if _state["running"] and not _reserved:
            return {"ok": False, "msg": "已有训练任务在跑"}
        _state = {"running": True, "stage": "loading", "progress": 0.0, "log": []}

    # 训练会吃满显存，和 OCR / 书库导入同时跑会互相拖慢甚至 OOM
    try:
        from . import library as _lib, ocr as _ocr

        if _ocr.state().get("running"):
            _state["running"] = False
            return {"ok": False, "msg": "OCR 任务正在跑，等它结束再训练"}
        if _lib.state().get("running"):
            _state["running"] = False
            return {"ok": False, "msg": "书库导入任务正在跑，等它结束再训练"}
    except Exception:  # noqa: BLE001
        pass

    base = base or config.get("finetune_base")
    epochs = int(epochs or config.get("finetune_epochs", 2))
    lr = float(lr or config.get("finetune_lr", 1e-4))
    tag = Path(dataset_path).stem.replace("dataset_", "")[:40]

    # 磁盘闸门：基座下载 + adapter + 合并产物加起来要 20GB 以上，
    # 空间不够时会在写权重写到一半失败，留下损坏目录
    try:
        free = config.free_disk_gb()
        if free < 20:
            _state["running"] = False
            return {"ok": False,
                    "msg": f"磁盘剩余 {free}GB，微调至少需要 20GB（含 15GB 合并产物）"}
    except Exception:  # noqa: BLE001
        pass

    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            _emit(on_event, "warn", "未检测到 CUDA，将用 CPU 训练（极慢）")

        _emit(on_event, "stage", f"加载基座模型 {base}")
        _state["stage"] = "loading_model"
        tok = AutoTokenizer.from_pretrained(base)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token

        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=dtype, device_map="auto" if torch.cuda.is_available() else None,
        )
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()

        lora = LoraConfig(
            r=int(config.get("finetune_lora_r", 16)),
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"],
        )
        model = get_peft_model(model, lora)
        model.print_trainable_parameters()

        # 载入数据
        rows = []
        with open(dataset_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        if len(rows) < 20:
            raise RuntimeError(f"样本太少（{len(rows)} 条），至少需要 20 条")
        _emit(on_event, "info", f"训练样本 {len(rows)} 条")

        max_len = 1024
        enc: list[tuple[list[int], int]] = []
        for r in rows:
            text = _format_example(tok, r)
            ids = tok(text, truncation=True, max_length=max_len, padding=False)["input_ids"]
            if len(ids) < 16:
                continue
            # 记录 prompt 长度：训练时把这段的 loss mask 掉，
            # 只让模型学「怎么答」，而不是学「怎么复读问题」
            try:
                msgs = r.get("messages") or []
                prompt_text = _format_example(tok, {"messages": msgs[:-1]}) if len(msgs) > 1 else ""
                plen = len(tok(prompt_text, truncation=True, max_length=max_len,
                               padding=False)["input_ids"]) if prompt_text else 0
            except Exception:
                plen = 0
            # 至少留 4 个 token 参与 loss，避免整条被 mask 掉导致 loss=nan
            plen = max(0, min(plen, len(ids) - 4))
            enc.append((ids, plen))
        _emit(on_event, "info", f"有效样本 {len(enc)} 条，开始训练 {epochs} 轮")

        dev = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(dev)
        opt = torch.optim.AdamW(
            [p for p in model.parameters() if p.requires_grad], lr=lr
        )
        bs = 1
        grad_accum = 8
        total_steps = max(1, (len(enc) // (bs * grad_accum)) * epochs)
        step = 0
        t0 = time.time()
        model.train()

        import random

        # 断点续训：每轮结束存一次 adapter，中断后可从下一轮接着跑。
        # 只按「轮」存而不是按「步」，是为了避免恢复优化器状态带来的复杂度 ——
        # 一轮训练通常几分钟，重跑一轮的代价可以接受。
        ckpt_root = OUTPUTS / f"ckpt_{tag}"
        start_ep = 0
        if ckpt_root.exists():
            done_eps = sorted(
                int(p.name[2:]) for p in ckpt_root.glob("ep*")
                if p.is_dir() and p.name[2:].isdigit()
            )
            if done_eps and done_eps[-1] < epochs:
                last = done_eps[-1]
                try:
                    from peft import PeftModel

                    model = PeftModel.from_pretrained(
                        model, str(ckpt_root / f"ep{last}"), is_trainable=True
                    )
                    model.to(dev)
                    start_ep = last
                    _emit(on_event, "info",
                          f"发现第 {last} 轮 checkpoint，从第 {last+1} 轮继续训练")
                except Exception as e:  # noqa: BLE001
                    _emit(on_event, "warn", f"checkpoint 加载失败（{str(e)[:50]}），从头开始")
                    start_ep = 0

        for ep in range(start_ep, epochs):
            random.shuffle(enc)
            for i in range(0, len(enc) - bs + 1, bs):
                batch = enc[i : i + bs]
                ml = max(len(b[0]) for b in batch)
                input_ids = torch.tensor(
                    [b[0] + [tok.pad_token_id] * (ml - len(b[0])) for b in batch], device=dev
                )
                attn = torch.tensor(
                    [[1] * len(b[0]) + [0] * (ml - len(b[0])) for b in batch], device=dev
                )
                labels = input_ids.clone()
                labels[attn == 0] = -100
                # 只对 assistant 回答部分计算 loss，prompt 段全部屏蔽
                for j, (_, plen) in enumerate(batch):
                    if plen > 0:
                        labels[j, :plen] = -100
                out = model(input_ids=input_ids, attention_mask=attn, labels=labels)
                loss = out.loss / grad_accum
                loss.backward()
                if (i // bs + 1) % grad_accum == 0:
                    torch.nn.utils.clip_grad_norm_(
                        [p for p in model.parameters() if p.requires_grad], 1.0
                    )
                    opt.step()
                    opt.zero_grad()
                    step += 1
                    _state["progress"] = round(min(0.99, step / total_steps), 3)
                    if step % 5 == 0:
                        _state["stage"] = (
                            f"训练中 epoch {ep+1}/{epochs} · step {step}/{total_steps}"
                        )
                        _emit(
                            on_event, "train",
                            f"epoch {ep+1}/{epochs} · step {step}/{total_steps} · "
                            f"loss {out.loss.item():.4f} · {int(time.time()-t0)}s",
                            progress=_state["progress"],
                        )
            # 本轮结束，落盘 checkpoint
            try:
                ckpt_root.mkdir(parents=True, exist_ok=True)
                model.save_pretrained(str(ckpt_root / f"ep{ep+1}"))
                _emit(on_event, "info", f"第 {ep+1} 轮 checkpoint 已保存")
            except Exception as e:  # noqa: BLE001
                _emit(on_event, "warn", f"checkpoint 保存失败：{str(e)[:50]}")

        # 保存 adapter
        adapter_dir = OUTPUTS / f"adapter_{tag}_{int(time.time())}"
        model.save_pretrained(str(adapter_dir))
        tok.save_pretrained(str(adapter_dir))
        _emit(on_event, "ok", f"LoRA adapter 已保存：{adapter_dir}")

        # 合并成完整模型
        _state["stage"] = "merging"
        _emit(on_event, "stage", "合并 LoRA 到基座权重…")
        merged_dir = OUTPUTS / f"merged_{tag}_{int(time.time())}"
        merged_dir.mkdir(parents=True, exist_ok=True)
        try:
            from peft import PeftModel

            base_model = AutoModelForCausalLM.from_pretrained(
                base, torch_dtype=torch.float16,
                device_map="cpu" if not torch.cuda.is_available() else None,
            )
            pm = PeftModel.from_pretrained(base_model, str(adapter_dir))
            merged = pm.merge_and_unload()
            merged.save_pretrained(str(merged_dir), safe_serialization=True)
            tok.save_pretrained(str(merged_dir))
            # 打完成标记：没这个标记的 merged 目录视为半成品，列表里不显示
            (merged_dir / ".ok").write_text("1", encoding="utf-8")
            _emit(on_event, "ok", f"合并完成：{merged_dir}")
        except Exception as e:  # noqa: BLE001
            _emit(on_event, "warn", f"合并失败（adapter 仍可用）：{e}")
            # 清掉半成品，避免占 15GB 又不可用
            try:
                shutil.rmtree(merged_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

        _state.update(running=False, stage="done", progress=1.0)

        # 清理旧的 merged 模型：每个约 15GB，不清理几次就把磁盘撑爆。
        # 保留最近 2 个，够对比用；adapter 只有 165MB，全部保留。
        try:
            olds = sorted(
                [p for p in OUTPUTS.glob("merged_*") if p.is_dir()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            removed = 0
            for old in olds[2:]:
                shutil.rmtree(old, ignore_errors=True)
                removed += 1
            if removed:
                _emit(on_event, "info", f"已清理 {removed} 个旧 merged 模型（保留最近 2 个）")
        except Exception:  # noqa: BLE001
            pass

        return {
            "ok": True,
            "adapter": str(adapter_dir),
            "merged": str(merged_dir) if merged_dir.exists() else "",
            "steps": step,
            "elapsed": int(time.time() - t0),
        }
    except Exception as e:  # noqa: BLE001
        _state.update(running=False, stage="error")
        _emit(on_event, "error", f"训练失败：{e}")
        return {"ok": False, "msg": str(e)}


@tasks.exclusive("微调任务")
def train_async(dataset_path: str, on_event: Event | None = None, **kw) -> dict:
    with _lock:
        if _state["running"]:
            return {"ok": False, "msg": "已有微调任务在跑"}
        _state.update(running=True, stage="starting", progress=0., result=None)
    def _go() -> None:
        try:
            r = train(dataset_path, on_event=on_event, _reserved=True, **kw)
            # 存结果供前端轮询取：训练结束但页面不知道跑成没跑成
            _state["result"] = r
        except Exception as e:  # noqa: BLE001
            _state["result"] = {"ok": False, "msg": str(e)}
            _emit(on_event, "error", f"训练失败：{str(e)[:120]}")
        finally:
            _state["running"] = False

    try:
        threading.Thread(target=_go, daemon=True).start()
    except Exception:
        _state["running"] = False
        raise
    return {"ok": True, "msg": "训练已在后台启动"}


def state() -> dict:
    return dict(_state)


def list_outputs() -> list[dict]:
    if not OUTPUTS.exists():
        return []
    out = []
    for p in sorted(OUTPUTS.iterdir(), key=lambda x: -x.stat().st_mtime):
        if not p.is_dir():
            continue
        kind = "adapter" if p.name.startswith("adapter") else "merged"
        # 合并中途失败会留下空壳目录；有 .ok 标记或完整权重文件才算可用
        if kind == "merged" and not (
            (p / ".ok").exists() or (p / "model.safetensors").exists()
        ):
            continue
        if p.name.startswith("ckpt_"):
            continue
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / (1024 ** 3)
        out.append(
            {
                "name": p.name,
                "kind": kind,
                "size_gb": round(size, 2),
                "mtime": p.stat().st_mtime,
            }
        )
    return out


# --------------------------------------------------------------------------
# GGUF 导出：llama.cpp 转换脚本的版本固定
# --------------------------------------------------------------------------
#: 导出 GGUF 时会在本机执行 llama.cpp 仓库里的 Python 脚本。原先浅克隆的是
#: 上游 HEAD，等于「每次导出都执行一份未经审视的最新远程代码」，与已移除的
#: trust_remote_code 同类风险。现在只取固定 tag，并且克隆后用 `git rev-parse HEAD`
#: 比对固定提交：tag 被改指向、镜像被篡改都会在执行前被拦下。
#: b12359 即上游 v1.0.0 发布对应的提交（2026-11-20 用 `git ls-remote` 实网核对）。
LLAMA_CPP_REPO = "https://github.com/ggml-org/llama.cpp"
LLAMA_CPP_REF = "b12359"
LLAMA_CPP_SHA = "b1b770d90d94cad9e9ea5b993bdbc383a522fb28"

#: 转换脚本的 Python 依赖。脚本默认优先用仓库自带（已随固定提交校验）的 gguf-py，
#: pip 包只是兜底；仍加上下界，避免半年后重装拿到破坏性大版本。
GGUF_PIP_SPECS = ("gguf>=0.10,<1.0", "sentencepiece>=0.2,<1.0", "protobuf>=4.21,<8.0")

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")


def _llama_cpp_pin() -> tuple[str, str]:
    """返回 (tag/分支, 期望提交 sha)。

    允许用配置键 `llama_cpp_ref` / `llama_cpp_sha`（或环境变量
    `EVOMIND_LLAMA_CPP_REF` / `EVOMIND_LLAMA_CPP_SHA`）换版本，但必须两者一起给：
    只给 ref 不给 sha 等于又退回「信任远端当前指向」，直接拒绝。
    """
    ref = str(config.get("llama_cpp_ref") or os.environ.get("EVOMIND_LLAMA_CPP_REF") or "").strip()
    sha = str(config.get("llama_cpp_sha") or os.environ.get("EVOMIND_LLAMA_CPP_SHA") or "").strip().lower()
    if not ref and not sha:
        return LLAMA_CPP_REF, LLAMA_CPP_SHA
    if not ref or not sha:
        raise ValueError("自定义 llama.cpp 版本时必须同时提供 llama_cpp_ref 与 llama_cpp_sha（40 位提交哈希）")
    if not _SHA_RE.fullmatch(sha):
        raise ValueError("llama_cpp_sha 必须是 40 位十六进制提交哈希")
    if not _REF_RE.fullmatch(ref) or ".." in ref:
        raise ValueError("llama_cpp_ref 只能包含字母、数字与 . _ / -，且不能以符号开头")
    return ref, sha


def _git_head(path: Path) -> str | None:
    """目录里 git 检出的 HEAD 提交；不是 git 仓库、git 不可用或受控文件被改过时返回 None。"""
    try:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if head.returncode != 0:
            return None
        # 只认干净检出：受控文件被改过就与 HEAD 不再对应，sha 一致也不可信
        dirty = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=60,
        )
        if dirty.returncode != 0 or (dirty.stdout or "").strip():
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return (head.stdout or "").strip().lower() or None


def _rmtree(path: Path) -> None:
    """删除目录；git 的 pack 文件在 Windows 上是只读的，需先去掉只读位再重试。"""
    if not path.exists():
        return

    def _retry(func, target, *_):  # noqa: ANN001
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:  # pragma: no cover - 3.10/3.11
        shutil.rmtree(path, onerror=_retry)


def _ensure_llama_cpp(on_event: Event | None = None) -> Path:
    """取得与固定提交一致的 llama.cpp 检出目录；不一致就绝不执行其中脚本。

    先克隆到旁边的临时目录、校验通过才替换旧目录：网络失败或校验失败时
    旧目录原样保留（但本次不会执行它），不会先删掉用户手放的内容再失败。
    """
    ref, sha = _llama_cpp_pin()
    target = config.DATA / "llama.cpp"
    if target.exists():
        head = _git_head(target)
        if head == sha and (target / "convert_hf_to_gguf.py").exists():
            return target
        _emit(on_event, "info",
              f"本地 llama.cpp（{(head or '非干净 git 检出')[:12]}）与固定版本 {ref} 不符，重新获取…")
    else:
        _emit(on_event, "info", f"下载 llama.cpp 转换脚本（固定版本 {ref}）…")

    staging = target.with_name(target.name + ".part")
    _rmtree(staging)
    try:
        p = subprocess.run(
            ["git", "-c", "advice.detachedHead=false", "clone", "--depth", "1",
             "--branch", ref, "--", LLAMA_CPP_REPO, str(staging)],
            capture_output=True, text=True, timeout=900,
        )
    except FileNotFoundError as e:
        raise RuntimeError("未找到 git，无法下载 llama.cpp 转换脚本") from e
    except subprocess.TimeoutExpired as e:
        _rmtree(staging)
        raise RuntimeError("下载 llama.cpp 超时（15 分钟）") from e
    if p.returncode != 0:
        _rmtree(staging)
        raise RuntimeError(f"下载 llama.cpp {ref} 失败：{(p.stderr or '').strip()[-200:]}")
    head = _git_head(staging)
    if head != sha:
        _rmtree(staging)
        raise RuntimeError(
            f"llama.cpp 版本校验失败：{ref} 实际指向 {(head or '未知')[:12]}，"
            f"与固定提交 {sha[:12]} 不符。已删除下载内容并中止，未执行其中任何脚本"
        )
    if not (staging / "convert_hf_to_gguf.py").exists():
        _rmtree(staging)
        raise RuntimeError(f"llama.cpp {ref} 中缺少 convert_hf_to_gguf.py，已中止")
    if target.exists():
        if (target / ".git").exists():
            _rmtree(target)  # 本程序早先克隆的未固定版本
        else:
            # 不是本程序克隆的（可能是用户手放的），挪到一边而不是删掉
            aside = target.with_name(f"{target.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            target.rename(aside)
            _emit(on_event, "warn", f"原 llama.cpp 目录不是 git 检出，已移到 {aside.name}")
    staging.rename(target)
    _emit(on_event, "ok", f"llama.cpp 已就绪：{ref}（{sha[:12]}，已校验）")
    return target


def export_gguf(merged_dir: str, on_event: Event | None = None) -> dict:
    """把合并后的模型转成 GGUF 并注册进 Ollama（尽力而为）。"""
    src = Path(merged_dir)
    if not src.exists():
        return {"ok": False, "msg": "目录不存在"}
    try:
        _emit(on_event, "info", "尝试通过 llama.cpp 转换 GGUF…")
        llm_cpp = _ensure_llama_cpp(on_event)
        py = _python_exe()
        subprocess.run(
            [py, "-m", "pip", "install", "-q", *GGUF_PIP_SPECS],
            capture_output=True, timeout=600,
        )
        f16 = config.DATA / "finetune" / (src.name + ".f16.gguf")
        p = subprocess.run(
            [py, str(llm_cpp / "convert_hf_to_gguf.py"), str(src),
             "--outfile", str(f16), "--outtype", "f16"],
            capture_output=True, text=True, timeout=3600,
        )
        if p.returncode != 0:
            _emit(on_event, "warn", f"GGUF 转换失败：{p.stderr[-300:]}")
            return {"ok": False, "msg": p.stderr[-300:]}
        _emit(on_event, "ok", f"GGUF 已生成：{f16.name}（可用 LM Studio 直接加载）")
        return {"ok": True, "gguf": str(f16)}
    except Exception as e:  # noqa: BLE001
        _emit(on_event, "warn", f"GGUF 转换未完成：{e}")
        return {"ok": False, "msg": str(e)}
