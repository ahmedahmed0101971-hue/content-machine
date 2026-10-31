"""YouTube thumbnail 1280x720 (long videos)."""
from __future__ import annotations

from PIL import Image, ImageDraw

from .primitives import background, fit, font, glow, hex_rgb, pill, place, text_layer

WHITE = (245, 247, 251)
INK = (7, 15, 20)


def render_thumbnail(pkg: dict, theme: dict, out_path: str) -> None:
    W, H = 1280, 720
    th = pkg.get("thumbnail") or {}
    acc = {"accent": hex_rgb(theme["accent"]), "accent2": hex_rgb(theme["accent2"]), "alert": hex_rgb(theme["alert"]),
           "white": WHITE}
    img = background(W, H, hex_rgb(theme["bg_top"]), hex_rgb(theme["bg_bot"]))
    col = acc.get(th.get("color", "accent"), acc["accent"])
    g = glow(col, 520, 70)
    img.alpha_composite(g, (W // 2 - 520 + 200, H // 2 - 520))
    d = ImageDraw.Draw(img)
    # brand
    d.rounded_rectangle([48, 40, 92, 84], radius=10, fill=acc["accent"] + (255,))
    bf = font("black", 30)
    bb = bf.getbbox(theme["badge"])
    d.text((70 - (bb[2] - bb[0]) / 2 - bb[0], 62 - (bb[3] - bb[1]) / 2 - bb[1]), theme["badge"], font=bf, fill=INK + (255,))
    place(img, text_layer(theme["brand"], fit(theme["brand"], "xbold", 30, 700), WHITE), 104, 62, 1.0)
    kicker = th.get("kicker") or ""
    big = th.get("big") or pkg["youtube"]["title"][:18]
    small = th.get("small") or ""
    y = 190
    if kicker:
        place(img, pill(kicker.upper(), fit(kicker.upper(), "black", 44, 1100), WHITE, acc["alert"] + (255,), padx=30, pady=16), 60, y, 1.0)
        y += 70
    bfnt = fit(big, "black", 300, 1160)
    lay = text_layer(big, bfnt, col)
    place(img, lay, 52, y + lay.height / 2 - 10, 1.0)
    y += lay.height + 10
    if small:
        sf = fit(small, "black", 80, 1160)
        place(img, text_layer(small, sf, WHITE), 60, min(y + 40, H - 70), 1.0)
    img.convert("RGB").save(out_path, "JPEG", quality=90)
