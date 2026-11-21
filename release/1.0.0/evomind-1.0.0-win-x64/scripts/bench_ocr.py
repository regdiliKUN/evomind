"""OCR 性能基准：拆分「渲染」与「推理」耗时，并在跑的过程中采样 GPU 利用率。

用法：
    .venv/Scripts/python.exe scripts/bench_ocr.py --book "K:/书籍/xxx.pdf" --pages 150-160
    .venv/Scripts/python.exe scripts/bench_ocr.py --book "K:/书籍/xxx.pdf" --pages 150-160 --cuda

输出：每页 render/infer 毫秒、总吞吐（页/秒）、GPU 平均利用率。
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parse_pages(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    return out


class GpuSampler:
    """后台采样 nvidia-smi，统计 GPU 利用率/显存。"""

    def __init__(self, interval: float = 0.2) -> None:
        self.interval = interval
        self.samples: list[tuple[float, float]] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _query(self) -> tuple[float, float] | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode != 0:
                return None
            util, mem = out.stdout.strip().splitlines()[0].split(",")
            return float(util), float(mem)
        except Exception:
            return None

    def start(self) -> None:
        def run() -> None:
            while not self._stop.is_set():
                v = self._query()
                if v:
                    self.samples.append(v)
                self._stop.wait(self.interval)

        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._t:
            self._t.join(timeout=3)
        if not self.samples:
            return {"util_avg": -1, "util_max": -1, "mem_max": -1}
        utils = [s[0] for s in self.samples]
        mems = [s[1] for s in self.samples]
        return {
            "util_avg": round(statistics.mean(utils), 1),
            "util_max": round(max(utils), 1),
            "mem_max": round(max(mems), 0),
            "n": len(utils),
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="PDF 路径")
    ap.add_argument("--pages", default="1-10", help="页范围，如 150-160")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--cuda", action="store_true", help="让 RapidOCR 走 CUDA")
    ap.add_argument("--warmup", type=int, default=1)
    args = ap.parse_args()

    import pymupdf

    from backend import ocr as ocr_mod

    if args.cuda:
        # 让 ORT 找得到 torch 自带的 CUDA/cuDNN DLL
        torch_lib = ROOT / ".venv" / "Lib" / "site-packages" / "torch" / "lib"
        if torch_lib.exists():
            import os
            os.add_dll_directory(str(torch_lib))
            os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
        import onnxruntime as ort
        print("providers:", ort.get_available_providers())
        from rapidocr_onnxruntime import RapidOCR
        # RapidOCR 1.2.x 的参数更新器要求显式带上 model_path（空字符串=用自带模型）
        ocr_mod._ocr_engine = RapidOCR(
            det_use_cuda=True, det_model_path="",
            cls_use_cuda=True, cls_model_path="",
            rec_use_cuda=True, rec_model_path="",
        )
        det = ocr_mod._ocr_engine.text_detector.infer.session
        rec = ocr_mod._ocr_engine.text_recognizer.session.session
        print("det providers:", det.get_providers())
        print("rec providers:", rec.get_providers())

    pages = parse_pages(args.pages)
    doc = pymupdf.open(args.book)

    rows = []
    gpu = GpuSampler()
    t_all = time.time()

    for idx, pno in enumerate(pages):
        t0 = time.time()
        _png, arr = ocr_mod.render_page(doc, pno, args.dpi)
        t1 = time.time()
        text = ocr_mod.ocr_rapid(arr)
        t2 = time.time()
        rows.append((pno + 1, (t1 - t0) * 1000, (t2 - t1) * 1000, len(text)))
        if idx == args.warmup:
            gpu.start()  # 预热之后再开始采样
        print(f"  p{pno + 1:>3}  render {(t1 - t0) * 1000:6.0f}ms  "
              f"infer {(t2 - t1) * 1000:6.0f}ms  {len(text):>4} 字")

    stats = gpu.stop()
    total = time.time() - t_all
    n = len(rows)
    print("\n===== 汇总 =====")
    print(f"页数 {n} · 总耗时 {total:.1f}s · 吞吐 {n / total:.2f} 页/秒"
          f"（{n / total * 60:.0f} 页/分钟）")
    print(f"render 平均 {statistics.mean(r[1] for r in rows):.0f}ms · "
          f"infer 平均 {statistics.mean(r[2] for r in rows):.0f}ms")
    print(f"GPU 利用率 平均 {stats['util_avg']}% / 峰值 {stats['util_max']}% · "
          f"显存峰值 {stats['mem_max']}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
