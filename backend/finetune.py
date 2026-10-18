"""权重层进化：把知识库里的积累，用 LoRA 微调固化进模型参数。

与知识层（RAG）的区别：
  - 知识层：查资料，随时可增删，但每次回答都要检索，慢且受上下文限制。
  - 权重层：把知识压进参数，回答变成"本能"，不依赖检索。
两者叠加才是完整的进化。

显存策略：默认 bf16 LoRA + gradient checkpointing（不依赖 bitsandbytes，
Windows 上更稳），7B 模型在 24GB 卡上约 17GB 峰值。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
def check_env() -> dict:
    mods = {}
    for name in ("torch", "transformers", "peft", "datasets"):
        try:
            m = __import__(name)
            mods[name] = getattr(m, "__version__", "ok")
        except Exception:
            mods[name] = None
    ready = all(v for k, v in mods.items() if k != "datasets")
    info: dict[str, Any] = {"ready": ready, "modules": mods, "cuda": None, "gpu": None}
    try:
        import torch

        info["cuda"] = torch.cuda.is_available()
        if info["cuda"]:
            info["gpu"] = torch.cuda.get_device_name(0)
            info["vram_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 1
            )
    except Exception:
        pass
    return info


INSTALL_CMD = (
    "pip install torch --index-url https://download.pytorch.org/whl/cu124 && "
    "pip install transformers peft datasets accelerate sentencepiece protobuf"
)


def install_env(on_event: Event | None = None) -> dict:
    """安装微调依赖（体积较大，约 3GB）。"""
    py = str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe")
    steps = [
        ["-m", "pip", "install", "torch", "--index-url", "https://download.pytorch.org/whl/cu124"],
        ["-m", "pip", "install", "transformers", "peft", "datasets", "accelerate",
         "sentencepiece", "protobuf", "safetensors"],
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
                    ]
                },
                ensure_ascii=False,
            ) + "\n")
    _state.update(stage=f"完成：{len(pairs)} 条", progress=1.0)
    _emit(on_event, "ok", f"数据集已生成：{path.name}（{len(pairs)} 条）")
    return {"ok": True, "path": str(path), "count": len(pairs)}


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

    threading.Thread(target=go, daemon=True).start()
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
) -> dict:
    global _state
    with _lock:
        if _state["running"]:
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
        tok = AutoTokenizer.from_pretrained(base, trust_remote_code=True)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token

        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=dtype, device_map="auto" if torch.cuda.is_available() else None,
            trust_remote_code=True,
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
                base, torch_dtype=torch.float16, trust_remote_code=True,
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


def train_async(dataset_path: str, on_event: Event | None = None, **kw) -> dict:
    def _go() -> None:
        train(dataset_path, on_event=on_event, **kw)

    threading.Thread(target=_go, daemon=True).start()
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


def export_gguf(merged_dir: str, on_event: Event | None = None) -> dict:
    """把合并后的模型转成 GGUF 并注册进 Ollama（尽力而为）。"""
    src = Path(merged_dir)
    if not src.exists():
        return {"ok": False, "msg": "目录不存在"}
    try:
        _emit(on_event, "info", "尝试通过 llama.cpp 转换 GGUF…")
        llm_cpp = config.DATA / "llama.cpp"
        if not (llm_cpp / "convert_hf_to_gguf.py").exists():
            _emit(on_event, "info", "下载 llama.cpp 转换脚本…")
            subprocess.run(
                ["git", "clone", "--depth", "1",
                 "https://github.com/ggml-org/llama.cpp", str(llm_cpp)],
                capture_output=True, timeout=900, check=True,
            )
        py = str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe")
        subprocess.run(
            [py, "-m", "pip", "install", "-q", "gguf", "sentencepiece", "protobuf"],
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
