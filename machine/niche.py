"""Niche finder ("باحث النيشات"): vidIQ-style opportunity analysis.

You type one or more search phrases. The machine searches YouTube like a viewer would (yt-dlp, no quota), collects
every channel that ranks, then reads each channel's creation date and totals from the YouTube Data API with the
token we already have (channels.list = 1 quota unit per 50 channels). For the most promising channels it also reads
their latest uploads (2 units per channel).

It answers: who wins this search, how old are they, and did NEW channels manage to break in recently?
If small/new channels get big views here, the niche is open; if only 10-year-old giants rank, it's closed.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from . import config, db
from .ytanalyze import _iso_dur, _Log, _ydl, explain

# YouTube "sp" filters (protobuf, base64): sort / upload date
SP = {
    ("relevance", "any"): "", ("relevance", "year"): "EgIIBQ==", ("relevance", "month"): "EgIIBA==",
    ("relevance", "week"): "EgIIAw==",
    ("views", "any"): "CAM=", ("views", "year"): "CAMSAggF", ("views", "month"): "CAMSAggE", ("views", "week"): "CAMSAggD",
}
PERIOD_AR = {"any": "أي وقت", "year": "السنة دي", "month": "الشهر ده", "week": "الأسبوع ده"}
SORT_AR = {"relevance": "الأنسب", "views": "الأكثر مشاهدة"}


# ------------------------------------------------------------------ search (no quota)
def search_url(query: str, sort: str, period: str) -> str:
    sp = SP.get((sort, period), "")
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    return url + (f"&sp={quote_plus(sp)}" if sp else "")


def search(query: str, sort: str, period: str, limit: int) -> list[dict]:
    log = _Log()
    with _ydl({"extract_flat": "in_playlist", "playlistend": limit, "lazy_playlist": True}, log) as y:
        info = y.extract_info(search_url(query, sort, period), download=False) or {}
    out = []
    for e in info.get("entries") or []:
        if not e or not re.fullmatch(r"[\w-]{11}", str(e.get("id", ""))) or not e.get("channel_id"):
            continue
        out.append({
            "id": e["id"], "title": e.get("title") or "", "views": e.get("view_count"),
            "duration": e.get("duration"), "channel_id": e["channel_id"], "channel": e.get("channel") or "",
            "short": "/shorts/" in (e.get("url") or "") or (e.get("duration") or 999) <= 60,
            "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg", "query": query,
        })
    if not out and log.errors:
        raise RuntimeError(explain(log.errors[-1]))
    return out[:limit]


# ------------------------------------------------------------------ API (token we already have)
def _api():
    from .ytanalyze import _api_token_channel
    cid = _api_token_channel()
    if not cid:
        return None
    from .google.http import check, request
    from .google.oauth import auth_headers
    from .google.youtube import API, token_name

    def get(path, params):
        r = request("GET", f"{API}/{path}", params=params, headers=auth_headers(token_name(cid)))
        return check(r, "يوتيوب API")
    return get


def channels_info(ids: list[str]) -> dict[str, dict]:
    get = _api()
    if not get:
        return {}
    out = {}
    for i in range(0, len(ids), 50):
        data = get("channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50]),
                                "maxResults": 50})
        for it in data.get("items", []):
            sn, st = it.get("snippet", {}), it.get("statistics", {})
            th = sn.get("thumbnails", {})
            out[it["id"]] = {
                "title": sn.get("title"), "handle": sn.get("customUrl") or "", "created": sn.get("publishedAt"),
                "country": sn.get("country") or "", "description": (sn.get("description") or "")[:500],
                "avatar": (th.get("medium") or th.get("default") or {}).get("url", ""),
                "subs": None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
                "total_views": _int(st.get("viewCount")), "videos": _int(st.get("videoCount")),
                "uploads": (it.get("contentDetails", {}).get("relatedPlaylists") or {}).get("uploads"),
            }
    return out


def recent_uploads(uploads_playlist: str, n: int = 30) -> list[dict]:
    """Latest uploads with views and duration (2 quota units)."""
    get = _api()
    if not get or not uploads_playlist:
        return []
    pl = get("playlistItems", {"part": "contentDetails", "playlistId": uploads_playlist, "maxResults": min(n, 50)})
    ids = [x["contentDetails"]["videoId"] for x in pl.get("items", [])]
    if not ids:
        return []
    vs = get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids), "maxResults": 50})
    out = []
    for v in vs.get("items", []):
        dur = _iso_dur(v["contentDetails"].get("duration"))
        out.append({"id": v["id"], "title": v["snippet"].get("title"), "published": v["snippet"].get("publishedAt"),
                    "views": _int(v["statistics"].get("viewCount")), "duration": dur,
                    "short": (dur or 999) <= 180 and "#short" in (v["snippet"].get("title", "") + " " +
                                                                   v["snippet"].get("description", "")).lower()
                    or (dur or 999) <= 60})
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ scoring
def _med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def _parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def analyze(videos: list[dict], chans: dict[str, dict], new_months: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    new_cut = now - timedelta(days=30.4 * new_months)
    by_ch: dict[str, dict] = {}
    for v in videos:
        c = by_ch.setdefault(v["channel_id"], {"channel_id": v["channel_id"], "name": v["channel"], "hits": 0,
                                               "hit_views": [], "best": None, "queries": set()})
        c["hits"] += 1
        c["queries"].add(v["query"])
        if v.get("views") is not None:
            c["hit_views"].append(v["views"])
        if not c["best"] or (v.get("views") or 0) > (c["best"].get("views") or 0):
            c["best"] = v
    rows = []
    for cid, c in by_ch.items():
        info = chans.get(cid, {})
        created = _parse(info.get("created"))
        age_days = max((now - created).days, 1) if created else None
        subs, tv, nv = info.get("subs"), info.get("total_views"), info.get("videos")
        row = {
            "channel_id": cid, "name": info.get("title") or c["name"], "handle": info.get("handle", ""),
            "url": f"https://www.youtube.com/{info['handle']}" if info.get("handle") else f"https://www.youtube.com/channel/{cid}",
            "avatar": info.get("avatar", ""), "country": info.get("country", ""),
            "created": created.date().isoformat() if created else "", "age_days": age_days,
            "age_label": _age_label(age_days), "is_new": bool(created and created >= new_cut),
            "subs": subs, "total_views": tv, "videos": nv,
            "views_per_video": round(tv / nv) if tv and nv else None,
            "views_per_day": round(tv / age_days) if tv and age_days else None,
            "subs_per_day": round(subs / age_days, 1) if subs and age_days else None,
            "videos_per_week": round(nv / (age_days / 7), 2) if nv and age_days else None,
            "hits": c["hits"], "queries": sorted(c["queries"]),
            "hit_median_views": _med(c["hit_views"]), "best_video": c["best"],
            "uploads": info.get("uploads"),
        }
        row["momentum"] = _momentum(row)
        rows.append(row)
    rows.sort(key=lambda r: (r["momentum"] or 0), reverse=True)
    return {"channels": rows, "niche": niche_score(videos, rows, new_months)}


def _age_label(d):
    if not d:
        return "—"
    if d < 60:
        return f"{d} يوم"
    if d < 730:
        return f"{round(d / 30.4)} شهر"
    return f"{round(d / 365, 1)} سنة"


def _momentum(r: dict) -> float | None:
    """How fast a channel grew for its age (log scale, 0-100). Young + big = high."""
    if not r.get("views_per_day"):
        return None
    vpd = r["views_per_day"]
    youth = 1.0 if (r["age_days"] or 9999) <= 365 else 0.8 if r["age_days"] <= 730 else 0.6 if r["age_days"] <= 1825 else 0.45
    return round(min(100, 12 * math.log10(1 + vpd)) * youth + (10 if r.get("is_new") else 0), 1)


def niche_score(videos: list[dict], chans: list[dict], new_months: int) -> dict:
    views = [v["views"] for v in videos if v.get("views") is not None]
    n_v, n_c = len(videos), len(chans)
    known = [c for c in chans if c.get("created")]
    new = [c for c in known if c["is_new"]]
    new_winners = [c for c in new if (c.get("hit_median_views") or 0) >= max(10000, (_med(views) or 0) * 0.5)]
    giants = [c for c in chans if (c.get("subs") or 0) >= 1_000_000]
    old = [c for c in known if (c["age_days"] or 0) >= 5 * 365]
    new_video_views = [v["views"] for v in videos
                       if v.get("views") is not None and any(c["channel_id"] == v["channel_id"] for c in new)]
    share_new_views = round(100 * sum(new_video_views) / sum(views)) if views and sum(views) else 0
    giant_share = round(100 * sum(1 for v in videos if any(c["channel_id"] == v["channel_id"] for c in giants)) / n_v) if n_v else 0
    top_heavy = round(100 * sum(sorted(views, reverse=True)[:max(1, len(views) // 10)]) / sum(views)) if views and sum(views) else 0

    demand = min(100, round(18 * math.log10(1 + (_med(views) or 0))))                      # 10k→72, 100k→90
    openness = min(100, round(2.2 * share_new_views + 6 * len(new_winners)))
    competition = min(100, round(0.6 * giant_share + 0.4 * (100 * len(old) / len(known) if known else 50)))
    score = round(0.35 * demand + 0.45 * openness + 0.20 * (100 - competition))
    verdict = ("🟢 فرصة قوية: قنوات جديدة بتكسب هنا" if score >= 60 else
               "🟡 فرصة متوسطة: ممكن تدخل بمحتوى أحسن بكتير" if score >= 40 else
               "🔴 صعبة: الكبار القدام مسيطرين والقنوات الجديدة مش بتاخد مشاهدات")
    return {
        "score": score, "verdict": verdict, "demand": demand, "openness": openness, "competition": competition,
        "videos_seen": n_v, "channels_seen": n_c, "median_views": _med(views), "top10pct_share": top_heavy,
        "new_channels": len(new), "new_winners": len(new_winners), "new_months": new_months,
        "share_views_new_channels_pct": share_new_views, "giants": len(giants), "giant_results_pct": giant_share,
        "old_channels": len(old), "shorts_pct": round(100 * sum(1 for v in videos if v.get("short")) / n_v) if n_v else 0,
        "median_duration_min": round((_med([v["duration"] for v in videos if v.get("duration")]) or 0) / 60, 1),
        "explain": [
            f"الطلب {demand}/100: الفيديو العادي في نتايج البحث بيجيب {int(_med(views) or 0):,} مشاهدة.",
            f"الانفتاح {openness}/100: {len(new)} قناة عمرها أقل من {new_months} شهر ظهرت في النتايج، منهم {len(new_winners)} بتجيب مشاهدات قوية، وواخدين {share_new_views}% من مشاهدات النتايج.",
            f"المنافسة {competition}/100: {giant_share}% من النتايج لقنوات فوق مليون مشترك، و{len(old)} قناة عمرها 5 سنين أو أكتر.",
        ],
    }


# ------------------------------------------------------------------ job
def new_job(queries_text: str, sort: str, period: str, per_query: int, new_months: int, deep: int) -> str:
    qs = [q.strip() for q in re.split(r"[\n,؛;]+", queries_text or "") if q.strip()][:10]
    if not qs:
        raise ValueError("اكتب كلمة أو جملة بحث واحدة على الأقل")
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:5]
    job = {"id": jid, "queries": qs, "sort": sort if sort in SORT_AR else "relevance",
           "period": period if period in PERIOD_AR else "any", "per_query": max(10, min(int(per_query or 100), 300)),
           "new_months": max(1, min(int(new_months or 12), 60)), "deep": max(0, min(int(deep or 10), 30)),
           "status": "queued", "progress": "في الطابور", "created": time.time(), "error": ""}
    db.kv_set(f"nichejob:{jid}", job)
    db.kv_set("nichejobs", ([jid] + db.kv_get("nichejobs", []))[:50])
    return jid


def get_job(jid):
    return db.kv_get(f"nichejob:{jid}")


def list_jobs():
    return [j for j in (get_job(i) for i in db.kv_get("nichejobs", [])) if j]


def next_queued():
    for j in reversed(list_jobs()):
        if j["status"] == "queued":
            return j
    return None


def _save(job, **kw):
    job.update(kw)
    db.kv_set(f"nichejob:{job['id']}", job)


def reports_dir():
    return config.DATA_DIR / "niche_reports"


def run_job(job: dict) -> dict:
    _save(job, status="running", progress="ببحث في يوتيوب…", started=time.time())
    try:
        videos, warnings = [], []
        for q in job["queries"]:
            _save(job, progress=f"ببحث: {q}")
            try:
                videos += search(q, job["sort"], job["period"], job["per_query"])
            except Exception as e:
                warnings.append(f"«{q}»: {explain(str(e))}")
        seen, uniq = set(), []
        for v in videos:
            if (v["id"], v["query"]) not in seen:
                seen.add((v["id"], v["query"]))
                uniq.append(v)
        videos = uniq
        if not videos:
            raise RuntimeError("مالقيتش نتايج. " + (" · ".join(warnings) or "جرب كلمات تانية."))
        ids = sorted({v["channel_id"] for v in videos})
        _save(job, progress=f"بجيب بيانات {len(ids)} قناة (تاريخ الإنشاء والمشتركين)…")
        try:
            chans = channels_info(ids)
        except Exception as e:
            chans = {}
            warnings.append(f"بيانات القنوات: {str(e)[:150]}")
        if not chans:
            warnings.append("تاريخ إنشاء القنوات محتاج ربط قناة يوتيوب في الإعداد (الربط اللي عندنا كفاية).")
        res = analyze(videos, chans, job["new_months"])
        deep_targets = [c for c in res["channels"] if c.get("uploads")][: job["deep"]]
        for i, c in enumerate(deep_targets, 1):
            _save(job, progress=f"بحلل آخر فيديوهات أقوى القنوات: {i}/{len(deep_targets)}")
            try:
                ups = recent_uploads(c["uploads"])
            except Exception as e:
                warnings.append(f"{c['name']}: {str(e)[:100]}")
                continue
            now = datetime.now(timezone.utc)
            last30 = [u for u in ups if _parse(u["published"]) and (now - _parse(u["published"])).days <= 30]
            c["recent"] = {
                "last30_uploads": len(last30), "last30_median_views": _med([u["views"] for u in last30]),
                "recent_median_views": _med([u["views"] for u in ups]),
                "shorts_pct": round(100 * sum(1 for u in ups if u["short"]) / len(ups)) if ups else None,
                "median_duration_min": round((_med([u["duration"] for u in ups if u["duration"]]) or 0) / 60, 1),
                "last_upload": max((u["published"] for u in ups), default="")[:10],
                "top_recent": sorted(ups, key=lambda u: u["views"] or 0, reverse=True)[:3],
            }
        rep = {"job": {k: job[k] for k in ("id", "queries", "sort", "period", "per_query", "new_months")},
               "generated": datetime.now(timezone.utc).isoformat(), "warnings": warnings,
               "niche": res["niche"], "channels": res["channels"],
               "top_videos": sorted(videos, key=lambda v: v.get("views") or 0, reverse=True)[:60]}
        new_ids = {c["channel_id"] for c in res["channels"] if c.get("is_new")}
        rep["new_channel_videos"] = sorted([v for v in videos if v["channel_id"] in new_ids],
                                           key=lambda v: v.get("views") or 0, reverse=True)[:30]
        reports_dir().mkdir(parents=True, exist_ok=True)
        (reports_dir() / f"{job['id']}.json").write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
        try:
            export_xlsx(rep, reports_dir() / f"{job['id']}.xlsx")
        except Exception as e:
            warnings.append(f"Excel: {e}")
        _to_drive(job, rep)
        _save(job, status="done", progress=f"خلص: {len(res['channels'])} قناة · الفرصة {res['niche']['score']}/100",
              finished=time.time(), score=res["niche"]["score"])
        return rep
    except Exception as e:
        _save(job, status="failed", progress="فشل", error=str(e)[:400], finished=time.time())
        raise


def _to_drive(job, rep):
    try:
        if not (config.OFFLINE or config.load_token("drive")):
            return
        from .google.drive import get_drive
        from .pipeline import ensure_drive_folders
        drive = get_drive()
        root = ensure_drive_folders(drive, config.load_settings())["_system"]
        folder = drive.ensure_folder("niche_reports", root)
        slug = re.sub(r"[^\w-]+", "-", job["queries"][0])[:40].strip("-") or "niche"
        drive.write_json(folder, f"{slug}-{job['id']}.json", rep)
    except Exception as e:
        rep.setdefault("warnings", []).append(f"Drive: {str(e)[:150]}")


def load_report(jid):
    p = reports_dir() / f"{jid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def export_xlsx(rep, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "القنوات"
    ws.sheet_view.rightToLeft = True
    cols = [("name", "القناة"), ("url", "الرابط"), ("created", "اتعملت"), ("age_label", "عمرها"), ("is_new", "جديدة"),
            ("subs", "مشتركين"), ("total_views", "مشاهدات كلية"), ("videos", "فيديوهات"), ("views_per_video", "مشاهدات/فيديو"),
            ("views_per_day", "مشاهدات/يوم"), ("subs_per_day", "مشتركين/يوم"), ("videos_per_week", "فيديو/أسبوع"),
            ("hits", "ظهرت في النتايج"), ("hit_median_views", "وسيط مشاهدات نتايجها"), ("momentum", "قوة النمو"),
            ("country", "البلد")]
    ws.append([h for _, h in cols] + ["آخر 30 يوم: فيديوهات", "آخر 30 يوم: وسيط المشاهدات", "% شورتس"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F2937")
    for r in rep["channels"]:
        rc = r.get("recent") or {}
        ws.append([("✓" if r.get(k) else "") if k == "is_new" else r.get(k) for k, _ in cols] +
                  [rc.get("last30_uploads"), rc.get("last30_median_views"), rc.get("shorts_pct")])
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 30
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    s = wb.create_sheet("النيش")
    s.sheet_view.rightToLeft = True
    n = rep["niche"]
    for k, v in [("البحث", " | ".join(rep["job"]["queries"])), ("درجة الفرصة", n["score"]), ("الحكم", n["verdict"]),
                 ("الطلب", n["demand"]), ("الانفتاح", n["openness"]), ("المنافسة", n["competition"])] + \
            [(f"شرح {i + 1}", e) for i, e in enumerate(n["explain"])]:
        s.append([k, v])
    s.column_dimensions["A"].width = 18
    s.column_dimensions["B"].width = 110
    v = wb.create_sheet("أقوى الفيديوهات")
    v.sheet_view.rightToLeft = True
    v.append(["العنوان", "القناة", "المشاهدات", "المدة (د)", "شورت", "البحث", "الرابط"])
    for x in rep["top_videos"]:
        v.append([x["title"], x["channel"], x.get("views"), round((x.get("duration") or 0) / 60, 1), "✓" if x.get("short") else "",
                  x["query"], f"https://www.youtube.com/watch?v={x['id']}"])
    v.column_dimensions["A"].width = 70
    wb.save(path)
