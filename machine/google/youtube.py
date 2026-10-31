"""YouTube Data API (upload, thumbnail, channel info) + YouTube Analytics."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

from .. import config
from .http import GoogleError, check, request
from .oauth import auth_headers

API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"


def token_name(channel_id: str) -> str:
    return f"youtube_{channel_id}"


def channel_info(cid: str) -> dict:
    if config.OFFLINE:
        return {"id": "UC_offline", "title": f"Offline {cid}"}
    r = request("GET", f"{API}/channels", params={"part": "snippet", "mine": "true"}, headers=auth_headers(token_name(cid)))
    items = check(r, "قراءة بيانات القناة").get("items", [])
    if not items:
        raise GoogleError("الحساب ده مالوش قناة يوتيوب. اختار القناة الصح وقت الموافقة.")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def upload(cid: str, video_path: str, meta: dict, publish_at: datetime | None, public_now: bool) -> str:
    """Resumable upload. publish_at -> private + scheduled; public_now -> public; neither -> private."""
    status = {"selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}
    if public_now:
        status["privacyStatus"] = "public"
    else:
        status["privacyStatus"] = "private"
        if publish_at:
            status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "snippet": {"title": meta["title"], "description": meta.get("description", ""), "tags": meta.get("tags", []),
                    "categoryId": str(meta.get("category_id", "25")), "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": status,
    }
    if config.OFFLINE:
        rec = config.DATA_DIR / "offline_uploads.jsonl"
        vid = "OFF" + str(int(time.time() * 1000))[-8:]
        with open(rec, "a", encoding="utf-8") as f:
            f.write(json.dumps({"video_id": vid, "channel": cid, "file": video_path, "body": body}, ensure_ascii=False) + "\n")
        return vid
    size = os.path.getsize(video_path)
    headers = {**auth_headers(token_name(cid)), "Content-Type": "application/json; charset=UTF-8",
               "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}
    r = request("POST", f"{UPLOAD}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                headers=headers, json=body)
    if r.status_code == 400 and "containsSyntheticMedia" in r.text:
        body["status"].pop("containsSyntheticMedia", None)   # older API versions
        r = request("POST", f"{UPLOAD}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                    headers=headers, json=body)
    if r.status_code >= 400:
        check(r, "بدء رفع الفيديو")
    session = r.headers.get("Location")
    if not session:
        raise GoogleError("يوتيوب مرجّعش رابط الرفع")
    offset = 0
    for attempt in range(8):
        with open(video_path, "rb") as f:
            f.seek(offset)
            data = f.read()
        hdr = {**auth_headers(token_name(cid)), "Content-Length": str(len(data))}
        if offset:
            hdr["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
        try:
            r = request("PUT", session, data=data, headers=hdr, retries=0, timeout=1800)
        except GoogleError:
            r = None
        if r is not None and r.status_code in (200, 201):
            return r.json()["id"]
        if r is not None and r.status_code not in (308, 500, 502, 503, 504):
            check(r, "رفع الفيديو")
        # ask how much was received, then resume
        time.sleep(min(60, 5 * (attempt + 1)))
        q = request("PUT", session, headers={**auth_headers(token_name(cid)), "Content-Range": f"bytes */{size}",
                                             "Content-Length": "0"}, retries=2)
        if q.status_code in (200, 201):
            return q.json()["id"]
        rng = q.headers.get("Range")
        offset = int(rng.split("-")[1]) + 1 if rng else 0
    raise GoogleError("الرفع فشل بعد محاولات كتير")


def video_status(cid: str, video_id: str) -> dict:
    """{'privacyStatus', 'publishAt', 'uploadStatus', 'rejectionReason'...} as YouTube reports it now."""
    if config.OFFLINE:
        return {}
    r = request("GET", f"{API}/videos", params={"part": "status", "id": video_id}, headers=auth_headers(token_name(cid)))
    items = check(r, "قراءة حالة الفيديو").get("items", [])
    return items[0]["status"] if items else {}


def set_thumbnail(cid: str, video_id: str, jpg_path: str) -> None:
    if config.OFFLINE:
        return
    with open(jpg_path, "rb") as f:
        data = f.read()
    r = request("POST", f"{UPLOAD}/thumbnails/set", params={"videoId": video_id},
                headers={**auth_headers(token_name(cid)), "Content-Type": "image/jpeg"}, data=data)
    check(r, "رفع الصورة المصغرة (لازم القناة تكون متفعّلة بالتليفون)")


def analytics(cid: str, days: int = 28) -> dict:
    """Per-video stats for the last N days."""
    end = date.today()
    start = end - timedelta(days=days)
    if config.OFFLINE:
        return {"start": str(start), "end": str(end), "rows": []}
    params = {"ids": "channel==MINE", "startDate": str(start), "endDate": str(end),
              "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,likes",
              "dimensions": "video", "sort": "-views", "maxResults": 200}
    r = request("GET", ANALYTICS, params=params, headers=auth_headers(token_name(cid)))
    data = check(r, "سحب الإحصائيات")
    cols = [h["name"] for h in data.get("columnHeaders", [])]
    rows = [dict(zip(cols, row)) for row in data.get("rows", [])]
    ids = [r_["video"] for r_ in rows][:50]
    titles = {}
    if ids:
        v = request("GET", f"{API}/videos", params={"part": "snippet", "id": ",".join(ids)}, headers=auth_headers(token_name(cid)))
        for it in check(v, "قراءة عناوين الفيديوهات").get("items", []):
            titles[it["id"]] = {"title": it["snippet"]["title"], "published": it["snippet"]["publishedAt"]}
    for r_ in rows:
        r_.update(titles.get(r_["video"], {}))
    return {"start": str(start), "end": str(end), "generated": datetime.now(timezone.utc).isoformat(), "rows": rows}
