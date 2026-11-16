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
    """Template C (bold, high-contrast) is the default since 2026-11; "template": "B" keeps the old white one."""
    if str((pkg.get("thumbnail") or {}).get("template", "C")).upper() != "B":
        return render_bold(pkg, out_path)
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


# ------------------------------------------------------------------ template C: bold clickbait-style (honest)
STYLES = {
    #          bg left          bg right         pill bg        pill text      badge bg        big glyph
    "money":   ((8, 16, 40),    (120, 12, 24),   (220, 20, 40), (255, 255, 255), (0, 168, 80),  "$"),
    "alert":   ((22, 0, 0),     (6, 6, 16),      (255, 214, 0), (10, 10, 10),   (220, 20, 40),  "!"),
    "sports":  ((6, 22, 60),    (200, 70, 0),    (255, 120, 0), (255, 255, 255), (20, 90, 220), "#1"),
    "news":    ((10, 10, 30),   (70, 20, 110),   (255, 214, 0), (10, 10, 10),   (220, 20, 40),  "?"),
    "tech":    ((8, 8, 28),     (0, 110, 160),   (0, 200, 255), (5, 5, 20),     (120, 60, 255), "NEW"),
    "weather": ((4, 20, 50),    (0, 120, 140),   (255, 214, 0), (10, 10, 10),   (230, 90, 0),   "!"),
    "movies":  ((10, 0, 20),    (130, 0, 60),    (255, 214, 0), (10, 10, 10),   (220, 20, 40),  "★"),
}
YEL = (255, 214, 0)


def bold_fields(pkg: dict) -> dict:
    th = pkg.get("thumbnail") or {}
    top, value, words = _fields(pkg)
    style = str(th.get("style") or ("alert" if top in ("SCAM", "ALERT", "WARNING") else "money")).lower()
    if style not in STYLES:
        style = "money"
    label = str(th.get("tile_top") or top)
    label = label if any(c.islower() for c in label) else label.upper()      # keep "iOS", "PlayStation"
    return {"label": label, "value": value if value not in ("!", "") else "", "words": words, "style": style,
            "chip": str(th.get("chip") or "").upper(), "glyph": str(th.get("glyph") or STYLES[style][5])}


def _gradient(c1, c2):
    g = Image.new("RGB", (W, 1))
    for x in range(W):
        t = x / (W - 1)
        g.putpixel((x, 0), tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    return g.resize((W, H)).convert("RGBA")


def _lines(words: list[str]) -> list[list[str]]:
    """2 lines when there are 3-4 words: the hook (last words) gets its own line in yellow."""
    if len(words) <= 2:
        return [[w] for w in words]
    if len(words) == 3:
        return [words[:2], words[2:]]
    return [words[:2], words[2:]]


def render_bold(pkg: dict, out_path: str) -> None:
    f = bold_fields(pkg)
    bg1, bg2, pill_bg, pill_fg, badge_bg, _ = STYLES[f["style"]]
    img = _gradient(bg1, bg2)

    # giant glyph on the right, glowing, slightly rotated (visual hook, no stock photos, no people)
    gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gl)
    gf = _fit(gd, f["glyph"], "black", 500, 440, 560)
    gd.text((1010, 380), f["glyph"], font=gf, fill=badge_bg + (255,), anchor="mm", stroke_width=14, stroke_fill=(255, 255, 255, 255))
    glow = gl.filter(ImageFilter.GaussianBlur(28))
    img.alpha_composite(glow)
    img.alpha_composite(gl.rotate(-8, resample=Image.BICUBIC, center=(1010, 380)))

    d = ImageDraw.Draw(img)
    left, maxw = 54, 700
    # label pill
    pf = _fit(d, f["label"], "black", 46, 560, 52)
    pb = d.textbbox((0, 0), f["label"], font=pf)
    pw = pb[2] - pb[0] + 56
    d.rounded_rectangle([left, 38, left + pw, 110], radius=16, fill=pill_bg)
    d.text((left + pw / 2, 74), f["label"], font=pf, fill=pill_fg, anchor="mm")

    # headline: big, white + yellow hook, thick black outline (readable on a phone)
    lines = [" ".join(l) for l in _lines(f["words"])]
    size = 150 if f["value"] else 190
    room = 400 if f["value"] else 520
    for _ in range(40):
        fonts = [_fit(d, l, "black", size, maxw) for l in lines]
        hs = [d.textbbox((0, 0), l, font=ft)[3] - d.textbbox((0, 0), l, font=ft)[1] for l, ft in zip(lines, fonts)]
        if sum(hs) + 22 * (len(lines) - 1) <= room:
            break
        size -= 6
    y = 140 if f["value"] else 120 + (560 - (sum(hs) + 22 * (len(lines) - 1))) / 2
    for k, (l, ft, h) in enumerate(zip(lines, fonts, hs)):
        top_off = d.textbbox((0, 0), l, font=ft)[1]
        d.text((left, y - top_off), l, font=ft, fill=YEL if k == len(lines) - 1 else (255, 255, 255),
               stroke_width=10, stroke_fill=(0, 0, 0))
        y += h + 22

    # value badge + optional chip + arrow
    if f["value"]:
        vf = _fit(d, f["value"], "black", 130, 520, 120)
        vb = d.textbbox((0, 0), f["value"], font=vf)
        bw, bh = vb[2] - vb[0] + 70, 150
        by = min(max(y + 14, 470), H - bh - 30)
        d.rounded_rectangle([left, by, left + bw, by + bh], radius=22, fill=badge_bg, outline=(255, 255, 255), width=7)
        d.text((left + bw / 2, by + bh / 2), f["value"], font=vf, fill=(255, 255, 255), anchor="mm",
               stroke_width=4, stroke_fill=tuple(max(0, c - 90) for c in badge_bg))
        cx = left + bw + 30
        if f["chip"]:
            cf = _fit(d, f["chip"], "black", 50, 300, 56)
            cb = d.textbbox((0, 0), f["chip"], font=cf)
            cw = cb[2] - cb[0] + 44
            d.rounded_rectangle([cx, by + 35, cx + cw, by + 115], radius=14, fill=(255, 255, 255))
            d.text((cx + cw / 2, by + 75), f["chip"], font=cf, fill=(220, 20, 40), anchor="mm")
            cx += cw
        # arrow from the glyph area to the badge
        x0, y0 = min(cx + 190, 860), by + bh + 10
        x1, y1 = cx + 20, by + bh / 2 + 10
        if x0 - x1 > 60:
            d.line([(x0, y0), (x1, y1)], fill=(255, 40, 40), width=20)
            import math
            a = math.atan2(y1 - y0, x1 - x0)
            L = 62
            d.polygon([(x1, y1), (x1 - L * math.cos(a - 0.5), y1 - L * math.sin(a - 0.5)),
                       (x1 - L * math.cos(a + 0.5), y1 - L * math.sin(a + 0.5))], fill=(255, 40, 40))
    img.convert("RGB").save(out_path, "JPEG", quality=92)
