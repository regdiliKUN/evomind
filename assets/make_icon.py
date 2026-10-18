"""生成 EvoMind 应用图标。

设计：圆角方块 + 三色对角渐变（蓝 → 紫 → 绿，对应「知识 → 进化 → 生长」）
+ 白色几何 E（与网页控制台 logo 同一套视觉语言）。

输出：
  assets/icon.ico   多尺寸 Windows 图标（16/24/32/48/64/128/256）
  assets/icon.png   512px 预览图
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent
S = 512
RADIUS = 112

# 品牌三色
C1 = np.array([77, 141, 255], dtype=np.float64)   # #4d8dff 蓝
C2 = np.array([169, 123, 255], dtype=np.float64)  # #a97bff 紫
C3 = np.array([33, 212, 160], dtype=np.float64)   # #21d4a0 绿


def gradient_layer(size: int) -> Image.Image:
    """对角三色渐变。"""
    y, x = np.mgrid[0:size, 0:size]
    # 沿对角线 t: 0(左上) -> 1(右下)，并稍微偏向垂直，视觉更稳
    t = (x / size) * 0.42 + (y / size) * 0.58
    t = np.clip(t, 0, 1)

    out = np.zeros((size, size, 3), dtype=np.float64)
    split = 0.55  # 蓝色区域占比更大，避免整张图偏紫
    low = t < split
    tt = (t / split)[..., None]
    out = np.where(low[..., None], C1 + (C2 - C1) * tt, out)
    tt2 = np.clip((t - split) / (1 - split), 0, 1)[..., None]
    out = np.where(low[..., None], out, C2 + (C3 - C2) * tt2)

    return Image.fromarray(out.astype(np.uint8), "RGB").convert("RGBA")


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw_glyph(img: Image.Image) -> None:
    """白色几何 E。横笔左端深入竖笔内部，让圆角完全被覆盖，不留接缝。"""
    d = ImageDraw.Draw(img)
    W = (255, 255, 255, 255)
    r = 15

    # 竖笔
    d.rounded_rectangle([140, 140, 202, 372], radius=r, fill=W)
    # 上横（左端 160 < 竖笔右缘 202，重叠 42px 远大于圆角 15px）
    d.rounded_rectangle([160, 140, 368, 202], radius=r, fill=W)
    # 中横（短一些，形成 E 的收腰）
    d.rounded_rectangle([160, 226, 332, 288], radius=r, fill=W)
    # 下横
    d.rounded_rectangle([160, 310, 368, 372], radius=r, fill=W)


def add_gloss(img: Image.Image) -> Image.Image:
    """左上角柔和高光，克制一点，别把品牌色冲淡。"""
    size = img.size[0]
    gloss = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(gloss)
    gd.ellipse([-size * 0.42, -size * 0.62, size * 0.72, size * 0.30], fill=20)
    gloss = gloss.filter(ImageFilter.GaussianBlur(size * 0.075))

    white = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    white.putalpha(gloss)
    return Image.alpha_composite(img, white)


def build() -> Image.Image:
    base = gradient_layer(S)
    base.putalpha(rounded_mask(S, RADIUS))
    base = add_gloss(base)
    draw_glyph(base)
    return base


def main() -> None:
    icon = build()

    # 圆角外的像素必须完全透明
    icon.putalpha(Image.composite(icon.getchannel("A"),
                                  Image.new("L", (S, S), 0),
                                  rounded_mask(S, RADIUS)))

    icon.save(OUT / "icon.png")

    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    icon.save(OUT / "icon.ico", format="ICO", sizes=sizes)

    print(f"icon.ico  {len(sizes)} 个尺寸")
    print(f"icon.png  {S}x{S}")
    for p in (OUT / "icon.ico", OUT / "icon.png"):
        print(f"  {p.name}  {p.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
