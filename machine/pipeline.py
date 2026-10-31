"""The production line: import -> check -> (review) -> voice + render -> upload."""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db, events, factcheck, package
from .google import tts, youtube
from .google.drive import get_drive
from .render import audio
from .render.thumbnail import render_thumbnail
from .render.video import VideoRenderer, build_timeline, estimate_speech

# ------------------------------------------------------------------ import
def ensure_drive_folders(drive, s: dict) -> dict:
    folders = s["drive"].get("folders") or {}
    need = ["root", "_system"] + [f"{c}/packages" for c in s["channels"]]
    if not all(k in folders for k in need):
        folders = drive.ensure_structure(list(s["channels"].keys()))
        config.update_settings(lambda x: x["drive"].update({"folders": folders, "root_id": folders["root"]}))
    return folders


def import_from_drive() -> int:
    s = config.load_settings()
    drive = get_drive()
    folders = ensure_drive_folders(drive, s)
    seen = set(db.kv_get("imported_files", []))
    added = 0
    for cid, ch in s["channels"].items():
        if not ch.get("enabled"):
            continue
        for f in drive.list_files(folders[f"{cid}/packages"]):
            fkey = f"{f['id']}@{f.get('modifiedTime', '')}"
            if fkey in seen:
                continue
            seen.add(fkey)
            try:
                pkg = json.loads(drive.read_text(f))
            except Exception as e:
                events.error(f"ملف '{f['name']}' مش JSON صحيح: {e}", cid)
                continue
            pkg.setdefault("channel", cid)
            errs = package.validate(pkg, s)
            if errs:
                events.error(f"باكدج '{f['name']}' فيها مشاكل: " + " · ".join(errs[:4]), cid)
                if package.ID_RE.match(str(pkg.get("id", ""))) and pkg.get("format") in ("long", "short"):
                    db.pkg_insert(pkg, "failed", f["id"])
                    db.pkg_update(pkg["id"], error=" · ".join(errs))
                continue
            status = "awaiting" if pkg.get("awaiting") else "new"
            if db.pkg_insert(pkg, status, f["id"]):
                added += 1
                events.info(f"باكدج جديدة: {pkg['youtube']['title']}", cid)
            else:
                old = db.pkg_get(pkg["id"])
                if old and old["status"] in ("rejected", "failed", "needs_review"):
                    # Claude rewrote it (same id) after a rejection note -> replace and re-check
                    db.pkg_update(pkg["id"], data=pkg, status=status, error="", attempts=0, check_result=None,
                                  drive_file_id=f["id"], priority=pkg.get("priority", "normal"))
                    added += 1
                    events.info(f"نسخة جديدة بعد التعديل: {pkg['youtube']['title']}", cid)
    db.kv_set("imported_files", sorted(seen)[-5000:])
    return added


def import_local(path: str) -> str:
    """Add a package from a local file (dashboard upload / CLI / tests)."""
    s = config.load_settings()
    with open(path, encoding="utf-8") as f:
        pkg = json.load(f)
    errs = package.validate(pkg, s)
    if errs:
        raise ValueError(" · ".join(errs))
    status = "awaiting" if pkg.get("awaiting") else "new"
    if not db.pkg_insert(pkg, status):
        raise ValueError("الباكدج دي موجودة قبل كده (نفس الـid)")
    return pkg["id"]


# ------------------------------------------------------------------ check
def check_new() -> None:
    s = config.load_settings()
    for p in db.pkg_list(status="new"):
        pkg = p["data"]
        ch = s["channels"][p["channel"]]
        lim = ch.get("limits", {})
        cap = lim.get("max_long" if p["format"] == "long" else "max_short", 99)
        if p["priority"] != "urgent" and db.count_for_day(p["channel"], p["pkg_date"], p["format"]) > cap:
            db.pkg_update(p["id"], status="rejected", note="تعدّى الحد اليومي للقناة")
            events.warn(f"اترفض تلقائياً لأنه فوق الحد اليومي: {pkg['youtube']['title']}", p["channel"], notify=False)
            continue
        try:
            res = factcheck.run(pkg, ch.get("blocked_words", []))
        except Exception as e:
            res = {"ok": False, "auto_ok": False, "facts": [], "uncovered_numbers": [], "blocked_words": [],
                   "summary": f"الفحص ماكملش: {e}"}
        pub = package.publish_time(pkg, ch)
        auto = ch.get("auto_publish") and res["auto_ok"] and not s.get("emergency_stop")
        db.pkg_update(p["id"], check_result=res, publish_at=pub.isoformat(),
                      status="approved" if auto else "needs_review")
        if auto:
            events.info(f"عدّى الفحص واتعتمد آلياً: {pkg['youtube']['title']}", p["channel"])
        else:
            events.info(f"مستني مراجعتك: {pkg['youtube']['title']} · {res['summary']}", p["channel"], notify=True)


# ------------------------------------------------------------------ render
def workdir(pid: str) -> Path:
    d = config.WORK_DIR / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def render_package(p: dict, with_voice: bool = True, progress=None) -> dict:
    s = config.load_settings()
    pkg = p["data"]
    ch = s["channels"][p["channel"]]
    wd = workdir(p["id"])
    clips = []
    if with_voice:
        for k, sc in enumerate(pkg["scenes"]):
            wav = tts.synthesize(sc["vo"], ch["voice"]["name"], float(ch["voice"].get("rate", 1.0)))
            a, _ = audio.wav_to_array(wav)
            clips.append(audio.trim_silence(a))
        speech = [audio.duration(c) for c in clips]
    else:
        speech = [estimate_speech(sc["vo"], float(ch["voice"].get("rate", 1.0))) for sc in pkg["scenes"]]
        clips = [audio.placeholder_speech(x) for x in speech]
    tl = build_timeline(speech)
    r = VideoRenderer(pkg, ch["theme"], tl)
    silent = wd / "video_silent.mp4"
    r.render(str(silent), progress=progress)
    wav = wd / "voice.wav"
    audio.build_track(clips, tl, r.dur, str(wav))
    final = wd / ("final.mp4" if with_voice else "preview.mp4")
    audio.mux(str(silent), str(wav), str(final))
    silent.unlink(missing_ok=True)
    (wd / "captions.srt").write_text(srt(r.captions), encoding="utf-8")
    thumb = None
    if pkg["format"] == "long":
        thumb = wd / "thumbnail.jpg"
        render_thumbnail(pkg, ch["theme"], str(thumb))
    vdur, adur = audio.media_duration(str(final)), r.dur
    if abs(vdur - adur) > 0.5:
        raise RuntimeError(f"مدة الفيديو ({vdur:.1f}) مش مطابقة للتوقيت ({adur:.1f})")
    return {"video": str(final), "thumb": str(thumb) if thumb else None, "duration": vdur, "timeline": [t.__dict__ for t in tl]}


def srt(chunks: list[tuple]) -> str:
    def ts(x: float) -> str:
        ms = int(round(x * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s_, ms = divmod(ms, 1000)
        return f"{h:02}:{m:02}:{s_:02},{ms:03}"
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(chunks, 1))


def render_next() -> bool:
    p = db.pkg_claim_next("approved", "rendering")
    if not p:
        return False
    title = p["data"]["youtube"]["title"]
    t0 = time.time()
    try:
        out = render_package(p, with_voice=True)
        db.pkg_update(p["id"], status="ready", video_path=out["video"], thumb_path=out["thumb"], error="")
        events.info(f"الفيديو جاهز ({out['duration']:.0f} ث، اتعمل في {time.time() - t0:.0f} ث): {title}", p["channel"])
    except Exception as e:
        attempts = (p.get("attempts") or 0) + 1
        status = "approved" if attempts < 3 else "failed"
        db.pkg_update(p["id"], status=status, attempts=attempts, error=str(e)[:500])
        (events.warn if status == "approved" else events.error)(
            f"مشكلة في تجهيز '{title}' (محاولة {attempts}): {e}", p["channel"], notify=status == "failed")
    return True


# ------------------------------------------------------------------ upload
def upload_next() -> bool:
    s = config.load_settings()
    if s.get("emergency_stop"):
        return False
    for p in db.pkg_list(status="ready", limit=50):
        ch = s["channels"][p["channel"]]
        if not ch.get("youtube", {}).get("connected"):
            continue   # stays 'ready' -> downloadable from the dashboard
        if not db.conn().execute("UPDATE packages SET status='uploading' WHERE id=? AND status='ready'", (p["id"],)).rowcount:
            continue
        pkg = p["data"]
        now = datetime.now(timezone.utc)
        pub = datetime.fromisoformat(p["publish_at"]) if p.get("publish_at") else now
        audited = s.get("youtube_audited")
        public_now = audited and pub <= now + timedelta(minutes=5)
        schedule = pub if (audited and not public_now) else None
        try:
            vid = youtube.upload(p["channel"], p["video_path"], pkg["youtube"], schedule, public_now)
            if p.get("thumb_path"):
                try:
                    youtube.set_thumbnail(p["channel"], vid, p["thumb_path"])
                except Exception as e:
                    events.warn(f"الصورة المصغرة ماترفعتش: {e}", p["channel"], notify=False)
            st = "published" if public_now else ("scheduled" if schedule else "private")
            locked = False
            if audited:
                # Verify YouTube kept what we asked for (unaudited projects get locked to private).
                try:
                    vs = youtube.video_status(p["channel"], vid)
                    if vs and vs.get("privacyStatus") == "private" and not vs.get("publishAt"):
                        locked, st = True, "private"
                except Exception as e:
                    events.warn(f"مقدرتش أتأكد من حالة الفيديو على يوتيوب: {e}", p["channel"], notify=False)
            db.pkg_update(p["id"], status=st, youtube_id=vid, error="")
            if locked:
                config.update_settings(lambda x: x.update({"youtube_audited": False}))
                events.error("يوتيوب قفل الفيديو خاص (Private): المشروع لسه محتاج موافقة مراجعة الـAPI (Audit). "
                             f"رجّعت الرفع لوضع الخاص لحد الموافقة. الفيديو: {pkg['youtube']['title']}",
                             p["channel"], notify=True)
            elif not audited:
                events.warn(f"اترفع كفيديو خاص (Private) لحد ما جوجل توافق على مراجعة الـAPI: {pkg['youtube']['title']}",
                            p["channel"], notify=False)
            else:
                events.info(f"اترفع على يوتيوب ({'منشور' if public_now else 'مجدول'}): {pkg['youtube']['title']}",
                            p["channel"], notify=True)
        except Exception as e:
            attempts = (p.get("attempts") or 0) + 1
            db.pkg_update(p["id"], status="ready", attempts=attempts, error=str(e)[:500])
            events.error(f"الرفع فشل لـ '{pkg['youtube']['title']}': {e}", p["channel"], notify=attempts >= 2)
        return True
    return False


# ------------------------------------------------------------------ housekeeping & sync back to Drive
def cleanup(keep_days: int = 7) -> None:
    cutoff = time.time() - keep_days * 86400
    for p in db.pkg_list(status=("scheduled", "published", "private", "rejected", "failed"), limit=2000):
        if p["updated_at"] < cutoff:
            d = config.WORK_DIR / p["id"]
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
    db.prune()


def system_snapshot() -> dict:
    """What Claude reads at the start of each run (no secrets)."""
    s = config.load_settings()
    today = datetime.now(package.ET).date().isoformat()
    chans = {}
    for cid, ch in s["channels"].items():
        recent = db.pkg_list(channel=cid, limit=60)
        chans[cid] = {
            "name": ch["name"], "enabled": ch["enabled"], "auto_publish": ch["auto_publish"],
            "slots_et": ch["slots"], "limits": ch.get("limits", {}),
            "blocked_words": ch.get("blocked_words", []), "voice": ch["voice"],
            "youtube_connected": ch.get("youtube", {}).get("connected", False),
            "today_et": today,
            "recent": [{"id": p["id"], "title": p["data"]["youtube"]["title"], "slot": p["slot"], "date": p["pkg_date"],
                        "status": p["status"], "note": p["note"], "error": p["error"][:200]} for p in recent],
        }
    return {"generated": datetime.now(timezone.utc).isoformat(), "emergency_stop": s.get("emergency_stop", False),
            "youtube_audited": s.get("youtube_audited", False), "channels": chans}


def sync_to_drive(include_candidates: bool = True) -> None:
    from . import radar
    s = config.load_settings()
    drive = get_drive()
    folders = ensure_drive_folders(drive, s)
    drive.write_json(folders["_system"], "system_snapshot.json", system_snapshot())
    if include_candidates:
        for cid, ch in s["channels"].items():
            if ch.get("enabled"):
                drive.write_json(folders["_system"], f"candidates_{cid}.json", radar.candidates_snapshot(cid))


def sync_analytics() -> None:
    s = config.load_settings()
    drive = get_drive()
    folders = ensure_drive_folders(drive, s)
    for cid, ch in s["channels"].items():
        if ch.get("enabled") and ch.get("youtube", {}).get("connected"):
            try:
                data = youtube.analytics(cid)
                drive.write_json(folders["_system"], f"analytics_{cid}.json", data)
                db.kv_set(f"analytics:{cid}", data)
            except Exception as e:
                events.warn(f"الإحصائيات ماتسحبتش: {e}", cid, notify=False)
