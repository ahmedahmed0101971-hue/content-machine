"""Official thumbnail (template B, approved by the owner): white background,
a calendar-style tile on the left (top label + big value), up to 4 big words on the right,
the last word in red with a yellow underline. 1280x720 JPEG.

Package field:
  "thumbnail": {"tile_top": "NOV", "tile_value": "10", "words": ["YOUR", "CHECK", "COMES", "EARLY"]}
Older fields (kicker / big / small) are still accepted.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

from .primitives import font

W, H = 1280, 720
WHITE = (255, 255, 255)
BLACK = (15, 17, 22)
RED = (229, 36, 47)
YELLOW = (255, 214, 0)
EDGE = (225, 228, 234)


def _fit(d: ImageDraw.ImageDraw, txt: str, kind: str, size: int, maxw: int, maxh: int | None = None):
    while size > 20:
        f = font(kind, size)
        b = d.textbbox((0, 0), txt, font=f)
        if b[2] - b[0] <= maxw and (maxh is None or b[3] - b[1] <= maxh):
            return f
        size -= 4
    return font(kind, size)


def _fields(pkg: dict) -> tuple[str, str, list[str]]:
    th = pkg.get("thumbnail") or {}
    top = th.get("tile_top") or th.get("kicker") or "NEW"
    value = th.get("tile_value") or th.get("big") or "!"
    words = th.get("words")
    if not words:
        src = th.get("small") or pkg.get("youtube", {}).get("title", "")
        words = [w for w in src.replace("·", " ").split() if w.isalpha()][:4] or ["WATCH", "THIS"]
    # never truncate: a cut value could change a number. Length limits are enforced in package.validate().
    return str(top).upper(), str(value), [str(w).upper() for w in words[:4]]


def render_thumbnail(pkg: dict, theme: dict, out_path: str) -> None:
    top, value, words = _fields(pkg)
    img = Image.new("RGBA", (W, H), WHITE)

    # tile with soft shadow
    x0, y0, x1, y1 = 60, 90, 560, 610
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x0 + 6, y0 + 14, x1 + 6, y1 + 14], radius=40, fill=(0, 0, 0, 70))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x0, y0, x1, y1], radius=40, fill=WHITE, outline=EDGE, width=4)
    d.rounded_rectangle([x0, y0, x1, y0 + 140], radius=40, fill=RED)
    d.rectangle([x0, y0 + 100, x1, y0 + 140], fill=RED)
    tf = _fit(d, top, "black", 96, x1 - x0 - 60, 100)
    d.text(((x0 + x1) / 2, y0 + 72), top, font=tf, fill=WHITE, anchor="mm")
    vf = _fit(d, value, "black", 300, x1 - x0 - 60, 330)
    d.text(((x0 + x1) / 2, y0 + 345), value, font=vf, fill=BLACK, anchor="mm")

    # words: same rhythm as the approved design, shrunk together if they don't fit
    x = 620
    maxw = W - x - 50
    n = len(words)
    sizes = [150] * n
    if n >= 1:
        sizes[-1] = 170
    gap = 26
    for _ in range(30):
        fonts = [_fit(d, w, "black", s, maxw) for w, s in zip(words, sizes)]
        boxes = [d.textbbox((0, 0), w, font=f) for w, f in zip(words, fonts)]
        total = sum(b[3] - b[1] for b in boxes) + gap * (n - 1) + 40
        if total <= H - 120:
            break
        sizes = [int(s * 0.92) for s in sizes]
    y = (H - total) / 2 + 6
    for k, (w, f, b) in enumerate(zip(words, fonts, boxes)):
        last = k == n - 1
        d.text((x, y - b[1]), w, font=f, fill=RED if last else BLACK)
        y += (b[3] - b[1]) + gap
    lw = min(360, boxes[-1][2] - boxes[-1][0])
    d.rectangle([x, y - gap + 12, x + lw, y - gap + 30], fill=YELLOW)

    img.convert("RGB").save(out_path, "JPEG", quality=92)
