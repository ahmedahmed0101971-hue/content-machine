"""Drawing primitives shared by the video templates and the thumbnail."""
from __future__ import annotations

import math
import threading

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .. import config

FONT_FILES = {
    "black": "InterDisplay-Black.otf",
    "xbold": "InterDisplay-ExtraBold.otf",
    "bold": "Inter-Bold.otf",
    "semi": "Inter-SemiBold.otf",
    "med": "Inter-Medium.otf",
}


_tls = threading.local()


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """Per-thread font cache: FreeType faces must not be shared between render threads."""
    cache = getattr(_tls, "fonts", None)
    if cache is None:
        cache = _tls.fonts = {}
    key = (kind, int(size))
    f = cache.get(key)
    if f is None:
        f = cache[key] = ImageFont.truetype(str(config.FONTS_DIR / FONT_FILES[kind]), int(size))
    return f


def fit(txt: str, kind: str, size: int, maxw: int) -> ImageFont.FreeTypeFont:
    size = int(size)
    while size > 16:
        f = font(kind, size)
        b = f.getbbox(txt)
        if b[2] - b[0] <= maxw:
            return f
        size -= 2
    return font(kind, size)


def hex_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def ease(x: float) -> float:
    x = clamp(x)
    return 1 - (1 - x) ** 3


def back(x: float) -> float:
    x = clamp(x)
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


class LayerCache:
    """Per-thread cache of pre-drawn text layers."""

    def __init__(self, limit: int = 800):
        self.local = threading.local()
        self.limit = limit

    def get(self, key, make):
        d = getattr(self.local, "d", None)
        if d is None:
            d = self.local.d = {}
        v = d.get(key)
        if v is None:
            v = make()
            if len(d) > self.limit:
                d.clear()
            d[key] = v
        return v


CACHE = LayerCache()


def text_layer(txt: str, f, color: tuple, alpha: int = 255) -> Image.Image:
    def make():
        b = f.getbbox(txt)
        pad = 16
        im = Image.new("RGBA", (max(1, b[2] - b[0] + 2 * pad), max(1, b[3] - b[1] + 2 * pad)), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((pad - b[0], pad - b[1]), txt, font=f, fill=tuple(color) + (alpha,))
        return im
    return CACHE.get(("t", txt, id(f), tuple(color), alpha), make)


def pill(txt: str, f, fg: tuple, bg: tuple, padx: int = 26, pady: int = 14, outline: tuple | None = None) -> Image.Image:
    def make():
        b = f.getbbox(txt)
        w, h = b[2] - b[0] + 2 * padx, b[3] - b[1] + 2 * pady
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=bg, outline=outline, width=2 if outline else 0)
        d.text((padx - b[0], pady - b[1]), txt, font=f, fill=tuple(fg) + (255,))
        return im
    return CACHE.get(("p", txt, id(f), tuple(fg), tuple(bg), outline, padx, pady), make)


def place(frame: Image.Image, layer: Image.Image, x: float, y: float, alpha: float,
          scale: float = 1.0, dx: float = 0, dy: float = 0, anchor: str = "lm") -> None:
    if alpha <= 0.01:
        return
    lay = layer
    if abs(scale - 1) > 0.01:
        lay = lay.resize((max(1, int(lay.width * scale)), max(1, int(lay.height * scale))), Image.BILINEAR)
    if alpha < 0.999:
        lay = lay.copy()
        lay.putalpha(lay.getchannel("A").point(lambda v: int(v * alpha)))
    ax = {"l": 0, "m": 0.5, "r": 1}[anchor[0]]
    ay = {"t": 0, "m": 0.5, "b": 1}[anchor[1]]
    frame.alpha_composite(lay, (int(x - lay.width * ax + dx), int(y - lay.height * ay + dy)))


def env(t: float, t0: float, end: float, dur: float = 0.45) -> float:
    """Fade in at t0, fade out just before end."""
    return ease((t - t0) / dur) * (1 - ease((t - (end - 0.35)) / 0.35))


def anim(frame, layer, x, y, t, t0, end, kind="up", anchor="lm", dur=0.45) -> None:
    p = clamp((t - t0) / dur)
    a = env(t, t0, end, dur)
    if kind == "up":
        place(frame, layer, x, y, a, dy=(1 - ease(p)) * 40, anchor=anchor)
    elif kind == "left":
        place(frame, layer, x, y, a, dx=-(1 - ease(p)) * 120, anchor=anchor)
    elif kind == "pop":
        place(frame, layer, x, y, a, scale=0.6 + 0.4 * back(p), anchor=anchor)
    else:
        place(frame, layer, x, y, a, anchor=anchor)


def background(w: int, h: int, top: tuple, bot: tuple) -> Image.Image:
    y = np.linspace(0, 1, h)[:, None, None]
    grad = (np.array(top) + (np.array(bot) - np.array(top)) * y).repeat(w, axis=1)
    img = Image.fromarray(grad.astype(np.uint8), "RGB").convert("RGBA")
    dots = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(dots)
    for gx in range(48, w, 48):
        for gy in range(48, h, 48):
            d.point((gx, gy), fill=(255, 255, 255, 22))
    img.alpha_composite(dots)
    return img


def glow(color: tuple, r: int, strength: int) -> Image.Image:
    yy, xx = np.mgrid[-r:r, -r:r]
    a = (np.clip(1 - np.sqrt(xx ** 2 + yy ** 2) / r, 0, 1) ** 2 * strength).astype(np.uint8)
    arr = np.zeros((2 * r, 2 * r, 4), np.uint8)
    arr[..., :3] = color
    arr[..., 3] = a
    return Image.fromarray(arr, "RGBA")


def wrap(txt: str, f, maxw: int) -> list[str]:
    words, lines, cur = txt.split(), [], ""
    for w_ in words:
        test = (cur + " " + w_).strip()
        if f.getbbox(test)[2] <= maxw or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def lerp(a: float, b: float, p: float) -> float:
    return a + (b - a) * p


def wobble(t: float, speed: float, amp: float) -> float:
    return math.sin(t * speed) * amp
