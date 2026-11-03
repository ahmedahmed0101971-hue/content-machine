"""Channel analyzer ("تحليل قناة"): every video of any public YouTube channel, no API key needed.

Uses yt-dlp to read the channel's Videos / Shorts / Live tabs, then (in "full" mode) each video page for exact
date, likes, comments, tags, description, chapters… If YouTube blocks the server, the missing details are filled
from the YouTube Data API with the channel token we already have (read-only, free quota).

Output: a JSON report (+ Excel workbook) in DATA_DIR/yt_reports and a copy in Drive (_system/channel_reports)
so Claude can read it.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

TABS = {"videos": "طويلة", "shorts": "شورتس", "streams": "لايف"}
MAX_VIDEOS = 3000
ET_NAME = "America/New_York"

STOP = set("""a an the and or but of to in on for with at by from is are was were be been it its this that these those
you your yours we our us i me my he she they them their his her what when where who why how which do does did not no yes
vs v s t ll re ve d m just so as if than then there here up out about into over after before more most very can will
""".split())


# ------------------------------------------------------------------ input
def channel_base(text: str) -> str:
    """'@name', 'youtube.com/@name/videos', '/channel/UC…', '/c/x', '/user/x' -> canonical base URL."""
    t = (text or "").strip()
    if not t:
        raise ValueError("اكتب لينك القناة أو @اسمها")
    if t.startswith("@"):
        return f"https://www.youtube.com/{t.split()[0]}"
    if re.fullmatch(r"UC[\w-]{22}", t):
        return f"https://www.youtube.com/channel/{t}"
    if not t.startswith("http"):
        t = "https://" + t
    m = re.search(r"youtube\.com/((?:@[^/?#\s]+)|(?:channel/UC[\w-]{22})|(?:c/[^/?#\s]+)|(?:user/[^/?#\s]+))", t)
    if not m:
        raise ValueError("ده مش لينك قناة يوتيوب. مثال: https://www.youtube.com/@MrBeast")
    return f"https://www.youtube.com/{m.group(1)}"


# ------------------------------------------------------------------ yt-dlp
class _Log:
    """Collects yt-dlp errors so the dashboard can say WHY nothing came back."""
    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errors.append(re.sub(r"\x1b\[[0-9;]*m", "", str(msg))[:300])


def explain(err: str) -> str:
    e = err.lower()
    if "sign in to confirm" in e or "bot" in e or "429" in e or "too many requests" in e:
        return "يوتيوب منع السيرفر مؤقتاً (فاكره روبوت). استنى ساعة وجرّب تاني، أو جرّب التحليل السريع."
    if "does not exist" in e or "404" in e or "not found" in e or "unable to recognize" in e:
        return "القناة مش موجودة. اتأكد من اللينك أو @الاسم."
    if "does not have a" in e and "tab" in e:
        return "القناة مافيهاش القسم ده."
    return err[:250]


def _ydl(extra: dict | None = None, log: _Log | None = None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True, "logger": log or _Log(),
            "ignore_no_formats_error": True, "socket_timeout": 30, "retries": 3, "extractor_retries": 2,
            "noplaylist": False}
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def list_tab(base: str, tab: str, limit: int) -> tuple[dict, list[dict]]:
    """Flat list of one channel tab: (channel info, entries)."""
    log = _Log()
    with _ydl({"extract_flat": "in_playlist", "playlistend": limit, "lazy_playlist": True,
               "extractor_args": {"youtubetab": {"approximate_date": [""]}}}, log) as y:
        info = y.extract_info(f"{base}/{tab}", download=False) or {}
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    if not entries and log.errors:
        raise RuntimeError(explain(log.errors[-1]))
    return info, entries[:limit]


def video_details(video_id: str) -> dict | None:
    with _ydl() as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


# ------------------------------------------------------------------ API fallback (token we already have)
def _api_token_channel() -> str | None:
    s = config.load_settings()
    for cid, ch in s["channels"].items():
        if ch.get("youtube", {}).get("connected"):
            return cid
    return None


def api_details(ids: list[str]) -> dict[str, dict]:
    """videos.list for up to 50 ids per call (1 quota unit each). {} if no connected channel."""
    cid = _api_token_channel()
    if not cid or not ids:
        return {}
    from .google.http import check, request
    from .google.oauth import auth_headers
    from .google.youtube import API, token_name
    out = {}
    for i in range(0, len(ids), 50):
        r = request("GET", f"{API}/videos", params={"part": "snippet,statistics,contentDetails,status",
                                                     "id": ",".join(ids[i:i + 50]), "maxResults": 50},
                    headers=auth_headers(token_name(cid)))
        for it in check(r, "قراءة بيانات الفيديوهات").get("items", []):
            sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
            out[it["id"]] = {
                "title": sn.get("title"), "description": sn.get("description"), "tags": sn.get("tags") or [],
                "published": sn.get("publishedAt"), "category_id": sn.get("categoryId"),
                "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                "view_count": _int(st.get("viewCount")), "like_count": _int(st.get("likeCount")),
                "comment_count": _int(st.get("commentCount")), "duration": _iso_dur(cd.get("duration")),
                "captions": cd.get("caption") == "true", "definition": cd.get("definition"),
                "thumbnail": ((sn.get("thumbnails") or {}).get("maxres") or (sn.get("thumbnails") or {}).get("high") or {}).get("url"),
            }
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _iso_dur(s: str | None) -> int | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


# ------------------------------------------------------------------ rows
def _best_thumb(e: dict) -> str:
    if e.get("thumbnail"):
        return e["thumbnail"]
    th = [t for t in (e.get("thumbnails") or []) if t.get("url")]
    th.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    return th[-1]["url"] if th else f"https://i.ytimg.com/vi/{e.get('id')}/hqdefault.jpg"


def _iso_from(e: dict) -> tuple[str, bool]:
    """(ISO date, exact?)"""
    if e.get("upload_date"):
        d = e["upload_date"]
        ts = e.get("timestamp") or e.get("release_timestamp")
        if ts:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat(), True
        return f"{d[:4]}-{d[4:6]}-{d[6:]}T12:00:00+00:00", True
    if e.get("published"):
        return e["published"], True
    if e.get("timestamp"):
        return datetime.fromtimestamp(e["timestamp"], timezone.utc).isoformat(), False   # approximate ("3 weeks ago")
    return "", False


def make_row(e: dict, kind: str) -> dict:
    desc = e.get("description") or ""
    title = e.get("title") or ""
    pub, exact = _iso_from(e)
    views, likes, comments = e.get("view_count"), e.get("like_count"), e.get("comment_count")
    dur = e.get("duration")
    row = {
        "id": e["id"], "type": kind, "title": title,
        "url": f"https://www.youtube.com/{'shorts/' if kind == 'shorts' else 'watch?v='}{e['id']}",
        "published": pub, "date_exact": exact,
        "views": views, "likes": likes, "comments": comments,
        "duration_sec": int(dur) if dur else None,
        "thumbnail": _best_thumb(e),
        "description": desc,
        "tags": e.get("tags") or [],
        "categories": e.get("categories") or ([e["category_id"]] if e.get("category_id") else []),
        "chapters": len(e.get("chapters") or []) or _desc_chapters(desc),
        "live_status": e.get("live_status") or "",
        "language": e.get("language") or "",
        "captions": bool(e.get("subtitles")) or bool(e.get("captions")),
        "age_limit": e.get("age_limit") or 0,
    }
    row.update(title_features(title))
    row["desc_len"] = len(desc)
    row["desc_links"] = len(re.findall(r"https?://", desc))
    row["hashtags"] = len(re.findall(r"(?<!\w)#\w+", desc + " " + title))
    return row


def _desc_chapters(desc: str) -> int:
    n = len(re.findall(r"(?m)^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s+\S", desc))
    return n if n >= 3 else 0


def title_features(t: str) -> dict:
    letters = [c for c in t if c.isalpha()]
    upper = sum(c.isupper() for c in letters)
    words = re.findall(r"[A-Za-z؀-ۿ0-9$%']+", t)
    return {
        "title_len": len(t), "title_words": len(words),
        "has_number": bool(re.search(r"\d", t)), "has_money": bool(re.search(r"\$|dollar|money|cash|paid|pay", t, re.I)),
        "has_question": "?" in t, "has_exclaim": "!" in t,
        "has_year": bool(re.search(r"\b20\d{2}\b", t)), "has_how": bool(re.search(r"\bhow\b|\bwhy\b|\bwhat\b", t, re.I)),
        "has_emoji": bool(re.search(r"[\U0001F300-\U0001FAFF☀-➿]", t)),
        "has_brackets": bool(re.search(r"[\(\[\|]", t)),
        "caps_ratio": round(upper / len(letters), 2) if letters else 0,
        "caps_words": sum(1 for w in words if len(w) > 2 and w.isupper()),
    }


# ------------------------------------------------------------------ analysis
def _median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 1) if xs else None


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def enrich(rows: list[dict], now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    et = ZoneInfo(ET_NAME)
    for kind in {r["type"] for r in rows}:
        same = [r for r in rows if r["type"] == kind]
        med = _median([r["views"] for r in same]) or 0
        for r in same:
            d = _parse_iso(r["published"])
            age = max((now - d).total_seconds() / 86400, 0.5) if d else None
            r["age_days"] = round(age, 1) if age else None
            r["views_per_day"] = round(r["views"] / age, 1) if (age and r["views"] is not None) else None
            r["engagement_pct"] = (round(100 * ((r["likes"] or 0) + (r["comments"] or 0)) / r["views"], 2)
                                   if r["views"] and (r["likes"] is not None or r["comments"] is not None) else None)
            r["vs_median"] = round(r["views"] / med, 2) if (med and r["views"] is not None) else None
            r["performance"] = ("🔥 فيرال" if (r["vs_median"] or 0) >= 3 else "✅ فوق المتوسط" if (r["vs_median"] or 0) >= 1.3
                                else "⚪ عادي" if (r["vs_median"] or 0) >= 0.6 else "🔻 تحت المتوسط") if r["vs_median"] is not None else ""
            if d and r["date_exact"]:
                local = d.astimezone(et)
                r["weekday_et"] = local.strftime("%A")
                r["hour_et"] = local.hour if (r["published"][11:19] != "12:00:00") else None
            else:
                r["weekday_et"], r["hour_et"] = "", None


def _words(titles: list[str]) -> Counter:
    c = Counter()
    for t in titles:
        seen = set()
        for w in re.findall(r"[a-z؀-ۿ0-9$']{3,}", t.lower()):
            if w not in STOP and w not in seen:
                c[w] += 1
                seen.add(w)
    return c


def _bigrams(titles: list[str]) -> Counter:
    c = Counter()
    for t in titles:
        ws = [w for w in re.findall(r"[a-z؀-ۿ0-9$']+", t.lower())]
        seen = set()
        for a, b in zip(ws, ws[1:]):
            if a in STOP and b in STOP:
                continue
            g = f"{a} {b}"
            if g not in seen:
                c[g] += 1
                seen.add(g)
    return c


def summarize(rows: list[dict], channel: dict) -> dict:
    out = {"channel": channel, "by_type": {}}
    for kind in [k for k in TABS if any(r["type"] == k for r in rows)]:
        rs = [r for r in rows if r["type"] == kind]
        views = [r["views"] for r in rs if r["views"] is not None]
        dated = sorted([r for r in rs if _parse_iso(r["published"])], key=lambda r: r["published"])
        span_days = ((_parse_iso(dated[-1]["published"]) - _parse_iso(dated[0]["published"])).days or 1) if len(dated) > 1 else None
        top = sorted(rs, key=lambda r: r["views"] or 0, reverse=True)
        n_top = max(1, math.ceil(len(rs) * 0.2))
        top20, bottom = top[:n_top], top[n_top:]
        recent30 = [r for r in rs if (r.get("age_days") or 1e9) <= 30]
        feats = ["has_number", "has_money", "has_question", "has_exclaim", "has_year", "has_how", "has_emoji",
                 "has_brackets"]

        def pct(group, f):
            return round(100 * sum(1 for r in group if r.get(f)) / len(group)) if group else None
        wc_top, wc_all = _words([r["title"] for r in top20]), _words([r["title"] for r in rs])
        lift = []
        for w, n in wc_top.most_common(80):
            share_top = n / len(top20)
            share_all = wc_all[w] / len(rs)
            if n >= 2 and share_all:
                lift.append({"word": w, "in_top": n, "in_all": wc_all[w], "lift": round(share_top / share_all, 2)})
        lift.sort(key=lambda x: (x["lift"], x["in_top"]), reverse=True)
        tags = Counter(t.lower() for r in rs for t in (r.get("tags") or []))
        wd = Counter(r["weekday_et"] for r in rs if r.get("weekday_et"))
        hrs = Counter(r["hour_et"] for r in rs if r.get("hour_et") is not None)
        by_month: dict[str, dict] = {}
        for r in dated:
            m = r["published"][:7]
            b = by_month.setdefault(m, {"videos": 0, "views": 0})
            b["videos"] += 1
            b["views"] += r["views"] or 0
        dur_top = _median([r["duration_sec"] for r in top20])
        dur_all = _median([r["duration_sec"] for r in rs])
        out["by_type"][kind] = {
            "label": TABS[kind], "count": len(rs), "total_views": sum(views), "median_views": _median(views),
            "mean_views": _mean(views), "max_views": max(views) if views else None,
            "median_likes": _median([r["likes"] for r in rs]), "median_comments": _median([r["comments"] for r in rs]),
            "median_engagement_pct": _median([r["engagement_pct"] for r in rs]),
            "median_duration_sec": dur_all, "median_duration_top_sec": dur_top,
            "first_upload": dated[0]["published"][:10] if dated else None,
            "last_upload": dated[-1]["published"][:10] if dated else None,
            "uploads_per_week": round(len(dated) / (span_days / 7), 2) if span_days else None,
            "last_30_days_uploads": len(recent30), "last_30_days_median_views": _median([r["views"] for r in recent30]),
            "viral_count": sum(1 for r in rs if (r.get("vs_median") or 0) >= 3),
            "top10": [{k: r.get(k) for k in ("title", "url", "views", "views_per_day", "published", "duration_sec",
                                              "thumbnail", "vs_median")} for r in top[:10]],
            "rising10": [{k: r.get(k) for k in ("title", "url", "views", "views_per_day", "published", "thumbnail")}
                         for r in sorted([r for r in rs if r.get("views_per_day")], key=lambda r: r["views_per_day"],
                                         reverse=True)[:10]],
            "title_patterns": [{"feature": f, "top20_pct": pct(top20, f), "rest_pct": pct(bottom, f)} for f in feats],
            "title_len_top": _median([r["title_len"] for r in top20]), "title_len_all": _median([r["title_len"] for r in rs]),
            "top_words": [{"word": w, "count": n} for w, n in wc_all.most_common(40)],
            "winning_words": lift[:25],
            "top_phrases": [{"phrase": g, "count": n} for g, n in _bigrams([r["title"] for r in top20]).most_common(25) if n >= 2],
            "top_tags": [{"tag": t, "count": n} for t, n in tags.most_common(40)],
            "weekday_uploads": dict(wd.most_common()),
            "weekday_median_views": {d: _median([r["views"] for r in rs if r.get("weekday_et") == d]) for d in wd},
            "hour_uploads_et": {str(h): n for h, n in sorted(hrs.items())},
            "by_month": by_month,
            "with_chapters_pct": pct(rs, "chapters"), "with_captions_pct": pct(rs, "captions"),
            "median_desc_len": _median([r["desc_len"] for r in rs]),
        }
    return out


# ------------------------------------------------------------------ job
REPORTS = lambda: config.DATA_DIR / "yt_reports"   # noqa: E731


def new_job(url: str, tabs: list[str], mode: str, limit: int) -> str:
    base = channel_base(url)
    tabs = [t for t in tabs if t in TABS] or ["videos"]
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:5]
    job = {"id": jid, "url": url, "base": base, "tabs": tabs, "mode": "full" if mode == "full" else "fast",
           "limit": max(1, min(int(limit or MAX_VIDEOS), MAX_VIDEOS)), "status": "queued", "progress": "في الطابور",
           "created": time.time(), "error": "", "channel_title": ""}
    db.kv_set(f"ytjob:{jid}", job)
    ids = db.kv_get("ytjobs", [])
    db.kv_set("ytjobs", ([jid] + ids)[:50])
    return jid


def get_job(jid: str) -> dict | None:
    return db.kv_get(f"ytjob:{jid}")


def list_jobs() -> list[dict]:
    return [j for j in (get_job(i) for i in db.kv_get("ytjobs", [])) if j]


def _save(job: dict, **kw):
    job.update(kw)
    db.kv_set(f"ytjob:{job['id']}", job)


def next_queued() -> dict | None:
    for j in reversed(list_jobs()):
        if j["status"] == "queued":
            return j
    return None


def run_job(job: dict) -> dict:
    _save(job, status="running", progress="بقرا القناة…", started=time.time())
    rows: list[dict] = []
    channel: dict = {}
    try:
        for tab in job["tabs"]:
            _save(job, progress=f"بجمع فيديوهات {TABS[tab]}…")
            try:
                info, entries = list_tab(job["base"], tab, job["limit"])
            except Exception as e:                       # a tab may not exist (no shorts, no lives)
                info, entries = {}, []
                job.setdefault("warnings", []).append(f"{TABS[tab]}: {explain(str(e))}")
            if info and not channel:
                channel = {k: info.get(k) for k in ("channel", "channel_id", "channel_url", "uploader_id",
                                                     "channel_follower_count", "description", "tags")}
                channel["title"] = info.get("channel") or info.get("uploader") or info.get("title", "").split(" - ")[0]
                _save(job, channel_title=channel["title"] or "")
            for e in entries:
                kind = "shorts" if tab == "shorts" or "/shorts/" in (e.get("url") or "") else (
                    "streams" if tab == "streams" else "videos")
                rows.append(make_row(e, kind))
        if not rows:
            why = " · ".join(job.get("warnings", [])) or "اتأكد من اللينك."
            raise RuntimeError(f"مالقيتش فيديوهات. {why}")
        if job["mode"] == "full":
            _fill_details(job, rows)
        enrich(rows)
        rep = {"job": {k: job[k] for k in ("id", "url", "base", "tabs", "mode", "limit")},
               "generated": datetime.now(timezone.utc).isoformat(), "videos": rows,
               "summary": summarize(rows, channel), "warnings": job.get("warnings", [])}
        REPORTS().mkdir(parents=True, exist_ok=True)
        path = REPORTS() / f"{job['id']}.json"
        path.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        try:
            export_xlsx(rep, REPORTS() / f"{job['id']}.xlsx")
        except Exception as e:
            job.setdefault("warnings", []).append(f"Excel: {e}")
        _to_drive(job, rep)
        _save(job, status="done", progress=f"خلص: {len(rows)} فيديو", finished=time.time(), count=len(rows))
        return rep
    except Exception as e:
        _save(job, status="failed", progress="فشل", error=str(e)[:400], finished=time.time())
        raise


def _fill_details(job: dict, rows: list[dict]) -> None:
    done = 0
    lock = threading.Lock()
    failed: list[dict] = []

    def one(r):
        nonlocal done
        try:
            d = video_details(r["id"])
        except Exception:
            d = None
        with lock:
            done += 1
            if done % 10 == 0 or done == len(rows):
                _save(job, progress=f"تفاصيل كل فيديو: {done}/{len(rows)}")
        if not d:
            failed.append(r)
            return
        kind = r["type"]
        new = make_row(d, kind)
        for k, v in new.items():
            if v not in (None, "", [], 0) or k in ("likes", "comments"):
                r[k] = v if v is not None else r.get(k)
        time.sleep(0.3)

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, rows))
    if failed:
        _save(job, progress=f"يوتيوب منع {len(failed)} فيديو، بكمّلهم من الـAPI…")
        try:
            api = api_details([r["id"] for r in failed])
        except Exception as e:
            api = {}
            job.setdefault("warnings", []).append(f"API: {str(e)[:150]}")
        for r in failed:
            a = api.get(r["id"])
            if a:
                new = make_row({"id": r["id"], **a}, r["type"])
                for k, v in new.items():
                    if v not in (None, "", []):
                        r[k] = v
                r["source"] = "api"
        miss = sum(1 for r in failed if r.get("source") != "api")
        if miss:
            job.setdefault("warnings", []).append(f"{miss} فيديو من غير تفاصيل كاملة (يوتيوب منع السيرفر ومفيش API)")


def _to_drive(job: dict, rep: dict) -> None:
    try:
        if not (config.OFFLINE or config.load_token("drive")):
            return
        from .google.drive import get_drive
        from .pipeline import ensure_drive_folders
        s = config.load_settings()
        drive = get_drive()
        root = ensure_drive_folders(drive, s)["_system"]
        folder = drive.ensure_folder("channel_reports", root)
        slug = re.sub(r"[^\w-]+", "-", (job.get("channel_title") or job["base"].rsplit("/", 1)[-1]))[:40].strip("-")
        light = dict(rep)
        light["videos"] = [{k: v for k, v in r.items() if k != "description"} | {"description": (r.get("description") or "")[:600]}
                           for r in rep["videos"]]
        drive.write_json(folder, f"{slug}-{job['id']}.json", light)
    except Exception as e:
        job.setdefault("warnings", []).append(f"Drive: {str(e)[:150]}")


# ------------------------------------------------------------------ excel
COLS = [("type", "النوع"), ("title", "العنوان"), ("url", "الرابط"), ("published", "تاريخ النشر"), ("views", "المشاهدات"),
        ("views_per_day", "مشاهدات/يوم"), ("vs_median", "× الوسيط"), ("performance", "الأداء"), ("likes", "لايك"),
        ("comments", "تعليقات"), ("engagement_pct", "تفاعل %"), ("duration_sec", "المدة (ث)"), ("weekday_et", "يوم النشر ET"),
        ("hour_et", "ساعة النشر ET"), ("title_len", "طول العنوان"), ("has_number", "رقم"), ("has_question", "سؤال"),
        ("has_year", "سنة"), ("caps_words", "كلمات CAPS"), ("tags", "التاجات"), ("chapters", "فصول"), ("hashtags", "هاشتاج"),
        ("desc_len", "طول الوصف"), ("desc_links", "لينكات الوصف"), ("description", "الوصف"), ("thumbnail", "الصورة المصغرة")]


def export_xlsx(rep: dict, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "الفيديوهات"
    ws.sheet_view.rightToLeft = True
    ws.append([c[1] for c in COLS])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F2937")
    for r in sorted(rep["videos"], key=lambda r: r.get("views") or 0, reverse=True):
        vals = []
        for k, _ in COLS:
            v = r.get(k)
            if isinstance(v, list):
                v = ", ".join(map(str, v))
            if k == "published" and v:
                v = v[:10]
            if isinstance(v, str) and len(v) > 32000:
                v = v[:32000]
            vals.append(v)
        ws.append(vals)
    widths = {"العنوان": 60, "الرابط": 18, "الوصف": 50, "الصورة المصغرة": 18, "التاجات": 30}
    for i, (_, h) in enumerate(COLS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(h, 12)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=c.column_letter in ("B",))
    s = wb.create_sheet("الملخص")
    s.sheet_view.rightToLeft = True
    s.append(["القناة", (rep["summary"]["channel"] or {}).get("title") or rep["job"]["base"]])
    s.append(["المشتركين", (rep["summary"]["channel"] or {}).get("channel_follower_count")])
    for kind, b in rep["summary"]["by_type"].items():
        s.append([])
        s.append([f"— {b['label']} —"])
        for k in ("count", "total_views", "median_views", "mean_views", "max_views", "median_likes", "median_comments",
                  "median_engagement_pct", "median_duration_sec", "median_duration_top_sec", "first_upload", "last_upload",
                  "uploads_per_week", "last_30_days_uploads", "last_30_days_median_views", "viral_count",
                  "title_len_top", "title_len_all", "with_chapters_pct", "median_desc_len"):
            s.append([k, b.get(k)])
        s.append([])
        s.append(["كلمات الفيديوهات الناجحة", "في الأفضل", "في الكل", "lift"])
        for w in b["winning_words"]:
            s.append([w["word"], w["in_top"], w["in_all"], w["lift"]])
        s.append([])
        s.append(["شكل العنوان", "أفضل 20%", "الباقي"])
        for p in b["title_patterns"]:
            s.append([p["feature"], p["top20_pct"], p["rest_pct"]])
        s.append([])
        s.append(["أكتر تاجات", "مرات"])
        for t in b["top_tags"][:25]:
            s.append([t["tag"], t["count"]])
    s.column_dimensions["A"].width = 34
    wb.save(path)


def load_report(jid: str) -> dict | None:
    p = REPORTS() / f"{jid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
