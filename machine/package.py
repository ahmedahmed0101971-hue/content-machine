"""Package format (what Claude writes to Drive for each video) + validation + publish time.

A package is one JSON file:
{
  "schema": 1,
  "id": "2026-11-02-money-longA-ssa-nov-dates",   # letters, digits, - and _ only
  "channel": "money",
  "format": "long" | "short",
  "slot": "longA",                # one of the channel's slots, or "urgent"
  "date": "2026-11-02",           # publish date, US Eastern
  "priority": "normal" | "urgent",
  "scenes": [ {"type": "...", "vo": "narration", ...fields per type} ],
  "facts": [ {"value": "Nov 3", "source": "https://...", "kind": "literal" | "derived", "note": "..."} ],
  "sources": [ {"name": "...", "url": "https://..."} ],
  "ticker": ["NOV 3 · ...", "..."],                 # optional (long videos)
  "youtube": {"title": "...", "description": "...", "tags": [...], "category_id": "25"},
  "thumbnail": {"kicker": "...", "big": "...", "small": "...", "color": "accent"},
  "awaiting": {"url": "...", "regex": "...", "placeholder": "{{VALUE}}"}   # optional
}
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCENE_TYPES = {
    "headline": ["lines"],
    "big_number": ["value"],
    "date_list": ["rows"],
    "before_after": ["before", "after"],
    "cards": ["cards"],
    "checklist": ["items"],
    "outro": ["domain"],
}
COLOR_NAMES = {"accent", "accent2", "alert", "white", "muted"}
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,120}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(pkg: dict, settings: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(pkg, dict):
        return ["الملف مش بصيغة JSON صحيحة"]
    pid = pkg.get("id", "")
    if not ID_RE.match(str(pid)):
        errs.append("المعرّف (id) ناقص أو فيه رموز غير مسموحة")
    ch = pkg.get("channel")
    if ch not in settings["channels"]:
        errs.append(f"القناة '{ch}' مش موجودة")
        return errs
    if pkg.get("format") not in ("long", "short"):
        errs.append("نوع الفيديو لازم يكون long أو short")
    slot = pkg.get("slot")
    if slot != "urgent" and slot not in settings["channels"][ch]["slots"]:
        errs.append(f"الميعاد '{slot}' مش من مواعيد القناة")
    if not DATE_RE.match(str(pkg.get("date", ""))):
        errs.append("التاريخ لازم يكون بالشكل YYYY-MM-DD")
    scenes = pkg.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errs.append("مفيش مشاهد في الفيديو")
        scenes = []
    if len(scenes) > 40:
        errs.append("عدد المشاهد كبير جداً (أقصى حد 40)")
    for i, sc in enumerate(scenes, 1):
        t = sc.get("type")
        if t not in SCENE_TYPES:
            errs.append(f"المشهد {i}: نوع غير معروف '{t}'")
            continue
        if not str(sc.get("vo", "")).strip():
            errs.append(f"المشهد {i}: مفيش نص للصوت (vo)")
        elif len(sc["vo"]) > 4000:
            errs.append(f"المشهد {i}: نص الصوت طويل جداً")
        for field in SCENE_TYPES[t]:
            if field not in sc or sc[field] in (None, "", []):
                errs.append(f"المشهد {i} ({t}): الحقل '{field}' ناقص")
    yt = pkg.get("youtube") or {}
    title = str(yt.get("title", ""))
    if not title:
        errs.append("مفيش عنوان للفيديو")
    elif len(title) > 100:
        errs.append("العنوان أطول من 100 حرف")
    if "<" in title or ">" in title:
        errs.append("العنوان فيه علامات < أو > (يوتيوب بيرفضها)")
    if len(str(yt.get("description", ""))) > 4900:
        errs.append("الوصف أطول من المسموح")
    tags = yt.get("tags", [])
    if not isinstance(tags, list) or len(",".join(tags)) > 480:
        errs.append("التاجات كتير (أقصى حد حوالي 480 حرف)")
    th = pkg.get("thumbnail") or {}
    if pkg.get("format") == "long":
        if not th.get("tile_value") or not th.get("words"):
            errs.append("الصورة المصغرة ناقصة (tile_value و words)")
        else:
            if len(str(th.get("tile_top", ""))) > 9:
                errs.append("الصورة المصغرة: الكلمة اللي فوق النتيجة أطول من 9 حروف")
            if len(str(th["tile_value"])) > 7:
                errs.append("الصورة المصغرة: الرقم في النتيجة أطول من 7 حروف")
            if not isinstance(th["words"], list) or not 1 <= len(th["words"]) <= 4 or any(len(str(w)) > 11 for w in th["words"]):
                errs.append("الصورة المصغرة: من 1 لـ4 كلمات، وكل كلمة 11 حرف بالكتير")
    if not pkg.get("sources"):
        errs.append("مفيش مصدر رسمي مذكور")
    for f in pkg.get("facts", []):
        if not f.get("value") or not f.get("source"):
            errs.append("فيه معلومة (fact) ناقصها القيمة أو المصدر")
            break
    return errs


def publish_time(pkg: dict, ch_cfg: dict, now: datetime | None = None) -> datetime:
    """UTC datetime the video should go public."""
    now = now or datetime.now(timezone.utc)
    slot = pkg.get("slot")
    if pkg.get("priority") == "urgent" or slot == "urgent":
        return now + timedelta(minutes=2)
    hhmm = ch_cfg["slots"].get(slot, "12:00")
    h, m = (int(x) for x in hhmm.split(":"))
    d = datetime.strptime(pkg["date"], "%Y-%m-%d").date()
    local = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
    return local.astimezone(timezone.utc)


def all_text(pkg: dict) -> str:
    """Every word that ends up on screen or in the voice (used by the checks)."""
    parts: list[str] = []

    def walk(x):
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k in ("color", "type", "cue", "source", "url"):
                    continue
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(pkg.get("scenes", []))
    walk(pkg.get("ticker", []))
    walk(pkg.get("thumbnail", {}))
    parts.append(str((pkg.get("youtube") or {}).get("title", "")))
    return "\n".join(parts)
