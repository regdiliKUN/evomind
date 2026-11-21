"""Serialize heavy task admission; reserve before workers start to avoid launch races."""
from functools import wraps
import threading

_lock = threading.RLock()
_pending: set[str] = set()


def busy_reason() -> str:
    from . import curriculum, evolve, finetune, library, ocr, refinement

    with _lock:
        if _pending:
            return next(iter(_pending)) + "正在启动或运行"
        engine_state = evolve.engine.state
        engine_label = "多模态摄取" if engine_state.get("mode") == "media" else "自主学习"
        for active, label in (
            (engine_state.get("running"), engine_label),
            (curriculum.is_active(), "方向轮换"),
            (refinement.state().get("running"), "知识炼化"),
            (library.state().get("running"), "书库任务"),
            (ocr.state().get("running") or ocr.state().get("batch"), "OCR"),
            (finetune.state().get("running"), "微调 / 训练集生成"),
        ):
            if active:
                return label + "正在运行"
    return ""


def exclusive(label):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            # _internal 是「同一任务内部再次准入」的通行证（方向轮换调用学习引擎），
            # 必须摘掉再转发，否则目标函数会收到一个它不认识的参数。
            if kwargs.pop("_internal", False):
                return fn(*args, **kwargs)
            with _lock:
                reason = busy_reason()
                if reason:
                    return {"ok": False, "msg": reason + "，请等待完成或先停止"}
                _pending.add(label)
            try:
                return fn(*args, **kwargs)
            finally:
                with _lock:
                    _pending.discard(label)
        return wrapped
    return decorate
