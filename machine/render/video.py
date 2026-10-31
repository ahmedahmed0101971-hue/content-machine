"""Video templates.

One renderer, two orientations:
  - long  -> 1920x1080 news layout (brand bar, ticker, captions)
  - short -> 1080x1920 vertical layout (brand pill, big captions)

Scene timing comes from the real narration audio: each scene lasts LEAD + speech + TAIL,
so slides change exactly when the voice moves to the next sentence block.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .primitives import (anim, back, background, clamp, ease, env, fit, font, glow, hex_rgb, pill, place,
                         text_layer, wrap)

FPS = 30
LEAD = 0.25     # silence before the narration of each scene
TAIL = 0.45     # pause after the narration of each scene
MIN_SCENE = 2.5
OUTRO_EXTRA = 1.2
CHARS_PER_SEC = 15.0   # used only when there is no audio (preview)

WHITE = (245, 247, 251)
MUTED = (150, 163, 190)
INK_DARK = (7, 15, 20)
CARD = (20, 32, 58)
LINE = (44, 60, 94)


@dataclass
class SceneTime:
    start: float
    end: float
    speech: float

    @property
    def speech_start(self) -> float:
        return self.start + LEAD


def build_timeline(speech_durations: list[float], last_is_outro: bool = True) -> list[SceneTime]:
    out, t = [], 0.0
    n = len(speech_durations)
    for i, sp in enumerate(speech_durations):
        dur = max(MIN_SCENE, LEAD + sp + TAIL)
        if last_is_outro and i == n - 1:
            dur += OUTRO_EXTRA
        out.append(SceneTime(t, t + dur, sp))
        t += dur
    return out


def estimate_speech(vo: str, rate: float = 1.0) -> float:
    return max(1.2, len(vo) / (CHARS_PER_SEC * rate))


class VideoRenderer:
    def __init__(self, pkg: dict, theme: dict, timeline: list[SceneTime]):
        self.pkg = pkg
        self.scenes = pkg["scenes"]
        self.tl = timeline
        self.vertical = pkg["format"] == "short"
        self.W, self.H = (1080, 1920) if self.vertical else (1920, 1080)
        self.dur = timeline[-1].end
        self.theme = theme
        self.c = {
            "accent": hex_rgb(theme["accent"]), "accent2": hex_rgb(theme["accent2"]), "alert": hex_rgb(theme["alert"]),
            "white": WHITE, "muted": MUTED,
        }
        top, bot = hex_rgb(theme["bg_top"]), hex_rgb(theme["bg_bot"])
        self.base = background(self.W, self.H, top, bot)
        self.glow1 = glow(self.c["accent"], 700, 55)
        self.glow2 = glow(self.c["accent2"], 520, 35)
        self.captions = self._caption_chunks()
        self.ticker_layer = None
        if not self.vertical and pkg.get("ticker"):
            txt = "      •      ".join(pkg["ticker"]) + "      •      "
            self.ticker_layer = text_layer(txt, font("semi", 28), WHITE)

    # ---------- helpers ----------
    def col(self, name: str | None, default: str = "accent") -> tuple:
        return self.c.get(name or default, self.c[default])

    def T(self, txt: str, kind: str, size: int, color: tuple, maxw: int | None = None):
        maxw = maxw or (self.W - 200)
        return text_layer(str(txt), fit(str(txt), kind, size, maxw), color)

    def cue_t(self, i: int, cue: str | None, k: int, n: int) -> float:
        tm, vo = self.tl[i], self.scenes[i].get("vo", "")
        if cue and cue.lower() in vo.lower():
            pos = vo.lower().index(cue.lower()) / max(1, len(vo))
            return max(tm.start + 0.2, tm.speech_start + tm.speech * pos - 0.15)
        span = max(0.6, tm.speech - 0.6)
        return tm.start + 0.3 + span * k / max(1, n)

    def _caption_chunks(self) -> list[tuple]:
        out = []
        for i, sc in enumerate(self.scenes):
            tm = self.tl[i]
            parts = [p.strip() for p in re.split(r"(?<=[.?!:;])\s+", sc.get("vo", "")) if p.strip()]
            merged: list[str] = []
            for p in parts:
                if merged and (len(merged[-1]) < 28 or len(p) < 14):
                    merged[-1] += " " + p
                else:
                    merged.append(p)
            total = sum(len(p) for p in merged) or 1
            t = tm.speech_start
            for p in merged:
                d = tm.speech * len(p) / total
                out.append((t, t + d, p))
                t += d
        return out

    def caption_layer(self, txt: str) -> Image.Image:
        from .primitives import CACHE

        def make():
            if self.vertical:
                f, maxw, lh = font("bold", 50), 900, 64
            else:
                f, maxw, lh = font("semi", 38), 1400, 50
            lines = wrap(txt, f, maxw)
            w = max(f.getbbox(l)[2] for l in lines) + 56
            h = lh * len(lines) + 28
            im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=16, fill=(5, 9, 18, 205))
            for k, l in enumerate(lines):
                lw = f.getbbox(l)[2]
                d.text(((w - lw) / 2, 12 + k * lh), l, font=f, fill=WHITE + (255,))
            return im
        return CACHE.get(("cap", self.vertical, txt), make)

    # ---------- chrome ----------
    def chrome(self, fr: Image.Image, t: float) -> None:
        W, H = self.W, self.H
        d = ImageDraw.Draw(fr)
        d.rectangle([0, 0, W, 6], fill=(255, 255, 255, 18))
        d.rectangle([0, 0, int(W * min(1, t / self.dur)), 6], fill=self.c["accent"] + (255,))
        th = self.theme
        if self.vertical:
            p = pill(f"{th['badge']}  {th['brand']}", font("bold", 30), WHITE, (255, 255, 255, 26))
            place(fr, p, W / 2, 120, 1.0, anchor="mm")
        else:
            d.rounded_rectangle([96, 46, 132, 82], radius=9, fill=self.c["accent"] + (255,))
            bf = font("black", 26)
            bb = bf.getbbox(th["badge"])
            d.text((114 - (bb[2] - bb[0]) / 2 - bb[0], 64 - (bb[3] - bb[1]) / 2 - bb[1]), th["badge"], font=bf, fill=INK_DARK + (255,))
            place(fr, self.T(th["brand"], "xbold", 30, WHITE, 900), 140, 64, 1.0)
            src = self.pkg.get("sources", [{}])[0]
            dom = re.sub(r"^https?://(www\.)?", "", src.get("url", "")).split("/")[0].upper()
            if dom:
                place(fr, pill(f"SOURCE: {dom}", font("bold", 24), WHITE, (255, 255, 255, 20), outline=LINE + (255,)),
                      W - 96, 64, 1.0, anchor="rm")
            if self.ticker_layer is not None:
                y0 = H - 66
                d.rectangle([0, y0, W, H], fill=(5, 9, 18, 255))
                lw = self.ticker_layer.width
                off = int((t * 120) % lw)
                for k in range(3):
                    fr.alpha_composite(self.ticker_layer, (260 - off + k * lw, y0 + 6))
                d.rectangle([0, y0, 240, H], fill=self.c["alert"] + (255,))
                lab = th.get("ticker_label", "LATEST")
                lf = fit(lab, "black", 28, 200)
                lb = lf.getbbox(lab)
                d.text((120 - (lb[2] - lb[0]) / 2 - lb[0], y0 + 17), lab, font=lf, fill=WHITE + (255,))
                d.line([(0, y0), (W, y0)], fill=LINE + (255,), width=2)
        cy = 1400 if self.vertical else (H - 140 if self.ticker_layer is not None else H - 90)
        for s, e, txt in self.captions:
            if s <= t < e:
                a = ease((t - s) / 0.15) * (1 - ease((t - (e - 0.1)) / 0.1))
                place(fr, self.caption_layer(txt), W / 2, cy, a, anchor="mm")
                break

    # ---------- frame ----------
    def frame_at(self, t: float) -> Image.Image:
        fr = self.base.copy()
        W, H = self.W, self.H
        fr.alpha_composite(self.glow1, (int(W * 0.28 + math.sin(t * 0.35) * 300 - 700), int(H * 0.4 + math.cos(t * 0.3) * 160 - 700)))
        fr.alpha_composite(self.glow2, (int(W * 0.78 + math.cos(t * 0.4) * 160 - 520), int(H * 0.7 + math.sin(t * 0.33) * 100 - 520)))
        i = next((k for k, tm in enumerate(self.tl) if tm.start <= t < tm.end), len(self.tl) - 1)
        sc = self.scenes[i]
        tm = self.tl[i]
        end = tm.end if i < len(self.tl) - 1 else tm.end + 10
        getattr(self, "s_" + sc["type"])(fr, t, sc, i, tm.start, end)
        self.chrome(fr, t)
        return fr.convert("RGB")

    def render(self, out_path: str, progress=None) -> None:
        n = int(math.ceil(self.dur * FPS))
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{self.W}x{self.H}",
               "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", os.environ.get("MACHINE_FFMPEG_THREADS", "2"),
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            for k in range(n):
                ff.stdin.write(self.frame_at(k / FPS).tobytes())
                if progress and k % 150 == 0:
                    progress(k / n)
        finally:
            ff.stdin.close()
            rc = ff.wait()
        if rc != 0:
            raise RuntimeError(f"ffmpeg exited with {rc}")

    # =====================================================================
    # Scene types
    # =====================================================================
    def s_headline(self, fr, t, sc, i, s, e):
        V = self.vertical
        lines = sc["lines"]
        if V:
            y = 430
            if sc.get("pill"):
                anim(fr, pill(sc["pill"], font("black", 34), WHITE, self.col(sc.get("pill_color"), "alert") + (255,)),
                     self.W / 2, y, t, s + 0.1, e, "pop", anchor="mm")
                y += 130
            maxl = 3 if sc.get("tile") else 5
            for k, ln in enumerate(lines[:maxl]):
                lay = self.T(ln["text"], "black", 118, self.col(ln.get("color"), "white"), 960)
                anim(fr, lay, self.W / 2, y, t, self.cue_t(i, ln.get("cue"), k, len(lines) + 1), e, anchor="mm")
                y += 135
            if sc.get("sub"):
                anim(fr, self.T(sc["sub"]["text"], "xbold", 62, self.col(sc["sub"].get("color"), "accent2"), 960),
                     self.W / 2, y + 10, t, self.cue_t(i, sc["sub"].get("cue"), len(lines), len(lines) + 1), e, anchor="mm")
                y += 110
            if sc.get("tile"):
                self._tile(fr, t, sc["tile"], self.W / 2, min(y + 180, 1250), s, e, scale=0.75)
        else:
            maxw = 1000 if sc.get("tile") else 1640
            n = len(lines[:4])
            block = n * 125 + (110 if sc.get("pill") else 0) + (115 if sc.get("sub") else 0)
            y = 470 - block / 2 + 40
            if sc.get("pill"):
                anim(fr, pill(sc["pill"], font("black", 28), WHITE, self.col(sc.get("pill_color"), "alert") + (255,)),
                     120, y, t, s + 0.1, e, "pop")
                y += 110
            for k, ln in enumerate(lines[:4]):
                lay = self.T(ln["text"], "black", 112, self.col(ln.get("color"), "white"), maxw)
                anim(fr, lay, 116, y, t, self.cue_t(i, ln.get("cue"), k, n + 1), e)
                y += 125
            if sc.get("sub"):
                anim(fr, self.T(sc["sub"]["text"], "xbold", 58, self.col(sc["sub"].get("color"), "accent2"), maxw),
                     120, y, t, self.cue_t(i, sc["sub"].get("cue"), n, n + 1), e)
            if sc.get("tile"):
                self._tile(fr, t, sc["tile"], 1500, 480, s, e)

    def _tile(self, fr, t, tile, cx, cy, s, e, scale=1.0):
        a = env(t, s + 0.5, e, 0.5)
        if a <= 0:
            return
        hdr_col = self.col(tile.get("header_color"), "alert")
        im = Image.new("RGBA", (420, 440), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, 419, 439], radius=36, fill=CARD + (255,), outline=LINE + (255,), width=3)
        d.rounded_rectangle([0, 0, 419, 110], radius=36, fill=hdr_col + (255,))
        d.rectangle([0, 70, 419, 110], fill=hdr_col + (255,))
        hf = fit(tile.get("header", ""), "black", 64, 380)
        hb = hf.getbbox(tile.get("header", ""))
        d.text(((420 - (hb[2] - hb[0])) / 2 - hb[0], 55 - (hb[3] - hb[1]) / 2 - hb[1]), tile.get("header", ""),
               font=hf, fill=(WHITE if sum(hdr_col) < 500 else INK_DARK) + (255,))
        flip_at = s + 2.6
        before = tile.get("from")
        val = str(tile.get("value", ""))
        show = before if (before and t < flip_at) else val
        col = WHITE if (before and t < flip_at) else self.col(tile.get("color"), "white")
        nf = fit(show, "black", 220, 380)
        layer = text_layer(show, nf, col)
        sq = 1.0
        if before and flip_at - 0.3 <= t < flip_at + 0.3:
            sq = abs(math.cos((t - (flip_at - 0.3)) / 0.6 * math.pi))
        layer = layer.resize((layer.width, max(1, int(layer.height * max(sq, 0.02)))))
        im.alpha_composite(layer, (int((420 - layer.width) / 2), int(275 - layer.height / 2)))
        place(fr, im, cx, cy, a, scale=scale * (0.85 + 0.15 * back((t - s - 0.5) / 0.5)), anchor="mm")

    def s_big_number(self, fr, t, sc, i, s, e):
        V = self.vertical
        v = sc["value"]
        pre, suf = sc.get("prefix", ""), sc.get("suffix", "")
        if isinstance(v, (int, float)):
            dec = int(sc.get("decimals", 1 if isinstance(v, float) and v != int(v) else 0))
            cur = v * ease((t - s - 0.4) / 1.4)
            txt = f"{pre}{cur:,.{dec}f}{suf}"
            final = f"{pre}{v:,.{dec}f}{suf}"
        else:
            txt = final = f"{pre}{v}{suf}"
        color = self.col(sc.get("color"), "accent")
        cards = sc.get("cards", [])[:3 if not V else 2]
        if V:
            if sc.get("label"):
                anim(fr, self.T(sc["label"], "bold", 44, MUTED, 960), self.W / 2, 520, t, s + 0.1, e, anchor="mm")
            nf = fit(final, "black", 300, 980)
            anim(fr, text_layer(txt, nf, color), self.W / 2, 740, t, s + 0.3, e, "pop", anchor="mm")
            if sc.get("caption"):
                anim(fr, self.T(sc["caption"], "black", 76, WHITE, 960), self.W / 2, 940, t, self.cue_t(i, sc.get("caption_cue"), 1, 3), e, anchor="mm")
            for k, cd in enumerate(cards):
                anim(fr, self._card(cd, 920, 200), self.W / 2, 1140 + k * 230, t, self.cue_t(i, cd.get("cue"), k + 2, len(cards) + 2), e, "left", anchor="mm")
        else:
            if sc.get("label"):
                anim(fr, self.T(sc["label"], "bold", 30, MUTED, 900), 120, 230, t, s + 0.1, e)
            maxw = 980 if cards else 1680
            nf = fit(final, "black", 260, maxw)
            anim(fr, text_layer(txt, nf, color), 110, 420, t, s + 0.3, e, "pop")
            if sc.get("caption"):
                anim(fr, self.T(sc["caption"], "semi", 46, WHITE, maxw), 120, 590, t, self.cue_t(i, sc.get("caption_cue"), 1, 3), e)
            for k, cd in enumerate(cards):
                anim(fr, self._card(cd, 700, 170), 1120, 330 + k * 200, t, self.cue_t(i, cd.get("cue"), k + 2, len(cards) + 2), e, "left")

    def _card(self, cd: dict, w: int, h: int) -> Image.Image:
        from .primitives import CACHE
        col = self.col(cd.get("color"), "accent")

        def make():
            im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=24, fill=CARD + (245,), outline=col + (255,), width=3)
            d.rounded_rectangle([0, 0, 12, h - 1], radius=6, fill=col + (255,))
            lab = str(cd.get("label", ""))
            val = str(cd.get("value", ""))
            lf = fit(lab, "bold", int(h * 0.18), w - 80)
            vf = fit(val, "black", int(h * 0.38), w - 80)
            d.text((44, int(h * 0.17)), lab, font=lf, fill=col + (255,))
            d.text((44, int(h * 0.44)), val, font=vf, fill=WHITE + (255,))
            return im
        return CACHE.get(("card", w, h, cd.get("label"), cd.get("value"), col), make)

    def s_date_list(self, fr, t, sc, i, s, e):
        V = self.vertical
        rows = sc["rows"][:5]
        n = len(rows)
        if V:
            if sc.get("title"):
                anim(fr, self.T(sc["title"], "bold", 44, MUTED, 960), self.W / 2, 420, t, s + 0.1, e, anchor="mm")
            y = 560
            for k, r in enumerate(rows):
                t0 = self.cue_t(i, r.get("cue"), k, n)
                p = pill(r["date"], font("black", 54), INK_DARK, self.col(r.get("color"), "accent") + (255,), padx=30, pady=16)
                anim(fr, p, 90, y, t, t0, e, "left")
                x2 = 90 + p.width + 24
                if r.get("weekday"):
                    anim(fr, self.T(r["weekday"], "bold", 30, MUTED, self.W - x2 - 60), x2, y - 26, t, t0 + 0.1, e, "left")
                anim(fr, self.T(r.get("text", ""), "semi", 40, WHITE, self.W - x2 - 60), x2, y + 22, t, t0 + 0.15, e, "left")
                y += 175
        else:
            has_cal = bool(sc.get("calendar"))
            if sc.get("title"):
                anim(fr, self.T(sc["title"], "bold", 30, MUTED, 900), 120, 230, t, s + 0.1, e)
            step = 130 if n <= 4 else 110
            y = 340
            for k, r in enumerate(rows):
                t0 = self.cue_t(i, r.get("cue"), k, n)
                p = pill(r["date"], font("black", 40), INK_DARK, self.col(r.get("color"), "accent") + (255,), padx=26, pady=14)
                anim(fr, p, 120, y, t, t0, e, "left")
                x2 = 120 + p.width + 18
                maxw = (960 - x2) if has_cal else (self.W - x2 - 140)
                if r.get("weekday"):
                    anim(fr, self.T(r["weekday"], "bold", 24, MUTED, maxw), x2, y - 18, t, t0 + 0.1, e, "left")
                anim(fr, self.T(r.get("text", ""), "semi", 34, WHITE, maxw), x2, y + 18, t, t0 + 0.15, e, "left")
                y += step
            if has_cal:
                self._calendar(fr, t, sc["calendar"], i, s, e)

    def _calendar(self, fr, t, cal, i, s, e):
        a = env(t, s + 0.2, e, 0.5)
        if a <= 0:
            return
        cw, ch = 118, 92
        days = int(cal.get("days", 30))
        first = int(cal.get("first_weekday", 0))  # 0 = Sunday
        nrows = (first + days + 6) // 7
        im = Image.new("RGBA", (7 * cw + 48, 70 + 46 + nrows * ch + 24), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=24, fill=CARD + (240,), outline=LINE + (255,), width=2)
        title = f"{cal.get('month', '')} {cal.get('year', '')}".strip().upper()
        d.text((28, 20), title, font=font("xbold", 34), fill=WHITE + (255,))
        for k, dn in enumerate("SMTWTFS"):
            f = font("bold", 22)
            bw = f.getbbox(dn)[2]
            d.text((24 + k * cw + (cw - bw) / 2, 80), dn, font=f, fill=MUTED + (255,))
        hl = {int(h["day"]): (self.cue_t(i, h.get("cue"), k, len(cal.get("highlights", []))), self.col(h.get("color"), "accent"))
              for k, h in enumerate(cal.get("highlights", []))}
        st = {int(x["day"]): self.cue_t(i, x.get("cue"), k, len(cal.get("strikes", []))) for k, x in enumerate(cal.get("strikes", []))}
        for day in range(1, days + 1):
            pos = first + day - 1
            r, c = pos // 7, pos % 7
            cx = 24 + c * cw + cw / 2
            cy = 116 + r * ch + ch / 2
            colr = MUTED
            if day in hl and t >= hl[day][0]:
                p = back((t - hl[day][0]) / 0.4)
                rad = 38 * p
                d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=hl[day][1] + (255,))
                colr = INK_DARK
            f = font("bold", 32)
            b = f.getbbox(str(day))
            d.text((cx - (b[2] - b[0]) / 2 - b[0], cy - (b[3] - b[1]) / 2 - b[1]), str(day), font=f, fill=colr + (255,))
            if day in st and t >= st[day]:
                p = ease((t - st[day]) / 0.4)
                d.line([(cx - 34, cy + 30), (cx - 34 + 68 * p, cy + 30 - 60 * p)], fill=self.c["alert"] + (255,), width=5)
        place(fr, im, 1010, 190, a, dy=(1 - ease((t - s - 0.2) / 0.5)) * 40, anchor="lt")

    def s_before_after(self, fr, t, sc, i, s, e):
        V = self.vertical
        W = self.W
        t_before = self.cue_t(i, sc.get("before_cue"), 0, 3)
        t_after = self.cue_t(i, sc.get("after_cue"), 1, 3)
        t_badge = self.cue_t(i, sc.get("badge_cue"), 1, 3)
        t_note = self.cue_t(i, sc.get("note_cue"), 2, 3)
        if sc.get("label"):
            anim(fr, self.T(sc["label"], "bold", 44 if V else 30, MUTED, W - 160), W / 2, 420 if V else 230, t, s + 0.1, e, anchor="mm")
        if V:
            bx, by, ax_, ay_, sz, arrow = W / 2, 700, W / 2, 1020, 170, "↓"
            ar_x, ar_y = W / 2, 860
        else:
            bx, by, ax_, ay_, sz, arrow = 520, 450, 1400, 450, 165, "→"
            ar_x, ar_y = W / 2, 440
        a1 = env(t, t_before, e)
        if a1 > 0:
            old = self.T(sc["before"], "black", sz, MUTED, 820 if not V else 960)
            place(fr, old, bx, by, a1, anchor="mm")
            if t > t_badge:
                d = ImageDraw.Draw(fr)
                p = ease((t - t_badge) / 0.4) * a1
                hw = old.width / 2 - 20
                d.line([(bx - hw, by + 20), (bx - hw + 2 * hw * p, by - 20)], fill=self.c["alert"] + (int(255 * a1),), width=12)
        anim(fr, self.T(arrow, "black", 150, WHITE), ar_x, ar_y, t, t_after - 0.3, e, "pop", anchor="mm")
        anim(fr, self.T(sc["after"], "black", sz, self.col(sc.get("after_color"), "accent2"), 820 if not V else 960),
             ax_, ay_, t, t_after, e, "pop", anchor="mm")
        if sc.get("badge"):
            anim(fr, pill(sc["badge"], font("bold", 34 if not V else 40), WHITE, self.col(sc.get("badge_color"), "alert") + (255,)),
                 W / 2, 640 if not V else 1200, t, t_badge, e, "up", anchor="mm")
        if sc.get("note"):
            anim(fr, self.T(sc["note"], "semi", 40 if not V else 44, WHITE, W - 200), W / 2, 740 if not V else 1310, t, t_note, e, "up", anchor="mm")

    def s_cards(self, fr, t, sc, i, s, e):
        V = self.vertical
        cards = sc["cards"][:4]
        n = len(cards)
        if sc.get("label"):
            anim(fr, self.T(sc["label"], "bold", 44 if V else 30, MUTED, self.W - 160),
                 self.W / 2 if V else 120, 420 if V else 230, t, s + 0.1, e, anchor="mm" if V else "lm")
        for k, cd in enumerate(cards):
            t0 = self.cue_t(i, cd.get("cue"), k, n)
            if V:
                anim(fr, self._card(cd, 920, 210), self.W / 2, 600 + k * 240, t, t0, e, "left", anchor="mm")
            else:
                if n <= 2:
                    x, y, w, h = 960, 380 + k * 230, 1100, 190
                    anim(fr, self._card(cd, w, h), x, y, t, t0, e, "left", anchor="mm")
                else:
                    col_, row_ = k % 2, k // 2
                    x, y = 120 + col_ * 860, 310 + row_ * 230
                    anim(fr, self._card(cd, 820, 190), x, y, t, t0, e, "left", anchor="lt")

    def s_checklist(self, fr, t, sc, i, s, e):
        V = self.vertical
        items = sc["items"][:5]
        n = len(items)
        if V:
            y = 430
            if sc.get("title"):
                anim(fr, self.T(sc["title"], "black", 84, WHITE, 960), self.W / 2, y, t, s + 0.1, e, anchor="mm")
                y += 150
            if sc.get("big"):
                anim(fr, self.T(sc["big"], "black", 200, self.col(sc.get("big_color"), "accent2"), 960), self.W / 2, y + 40, t, s + 0.4, e, "pop", anchor="mm")
                y += 260
            for k, it in enumerate(items):
                txt = it["text"] if isinstance(it, dict) else str(it)
                cue = it.get("cue") if isinstance(it, dict) else None
                anim(fr, self._check(txt, 900, 52), 90, y, t, self.cue_t(i, cue, k + 1, n + 1), e, "left")
                y += 120
        else:
            if sc.get("title"):
                anim(fr, self.T(sc["title"], "black", 84, WHITE, 1680), 120, 250, t, s + 0.1, e)
            y = 370
            if sc.get("big"):
                anim(fr, self.T(sc["big"], "black", 150, self.col(sc.get("big_color"), "accent2"), 1680), 116, y + 10, t, s + 0.4, e, "pop")
                y += 170
            for k, it in enumerate(items):
                txt = it["text"] if isinstance(it, dict) else str(it)
                cue = it.get("cue") if isinstance(it, dict) else None
                anim(fr, self._check(txt, 1600, 44), 120, y, t, self.cue_t(i, cue, k + 1, n + 1), e, "left")
                y += 92

    def _check(self, txt: str, w: int, size: int) -> Image.Image:
        from .primitives import CACHE

        def make():
            f = fit(txt, "semi", size, w - 120)
            b = f.getbbox(txt)
            h = max(90, b[3] - b[1] + 40)
            im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            r = 30
            cy = h / 2
            d.ellipse([10, cy - r, 10 + 2 * r, cy + r], fill=self.c["accent"] + (255,))
            d.line([(26, cy), (37, cy + 12), (56, cy - 12)], fill=INK_DARK + (255,), width=8)
            d.text((100, cy - (b[1] + b[3]) / 2), txt, font=f, fill=WHITE + (255,))
            return im
        return CACHE.get(("chk", txt, w, size, self.c["accent"]), make)

    def s_outro(self, fr, t, sc, i, s, e):
        V = self.vertical
        W = self.W
        y0 = 620 if V else 300
        anim(fr, self.T(sc.get("kicker", "OFFICIAL SOURCE"), "bold", 44 if V else 30, MUTED, W - 160), W / 2, y0, t, s + 0.1, e, anchor="mm")
        anim(fr, self.T(sc["domain"], "black", 150 if V else 170, WHITE, W - 160), W / 2, y0 + (170 if V else 150), t, s + 0.3, e, "pop", anchor="mm")
        if sc.get("cta"):
            cf = fit(sc["cta"], "bold", 48 if V else 40, W - 240)
            anim(fr, pill(sc["cta"], cf, INK_DARK, self.c["accent"] + (255,), padx=36, pady=20), W / 2, y0 + (370 if V else 320), t, s + 1.0, e, anchor="mm")
        disc = sc.get("disclaimer", "AI-narrated · facts checked against official sources · not financial advice")
        anim(fr, self.T(disc, "med", 30 if V else 26, MUTED, W - 160), W / 2, y0 + (500 if V else 430), t, s + 1.5, e, anchor="mm")
