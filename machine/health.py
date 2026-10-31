"""Full self-check ("فحص شامل"): one button in the dashboard instead of terminal commands.

The worker runs it (on request, and once a day), stores it in the DB, and writes it to Drive as
_system/health_report.json so Claude can read it directly.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone

from . import __version__, config, db

OK, WARN, ERR = "ok", "warn", "err"


def _row(name: str, status: str, detail: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail}


def _section(title: str, rows: list[dict]) -> dict:
    worst = ERR if any(r["status"] == ERR for r in rows) else WARN if any(r["status"] == WARN for r in rows) else OK
    return {"title": title, "status": worst, "rows": rows}


# ------------------------------------------------------------------ pieces
def check_system() -> dict:
    rows = []
    try:
        up = float(open("/proc/uptime").read().split()[0])
        rows.append(_row("السيرفر شغال من", WARN if up < 3600 else OK,
                         f"{up / 3600:.1f} ساعة" + (" (اتعمله restart قريب)" if up < 3600 else "")))
    except OSError:
        pass
    try:
        mem = {k.strip(): int(v.split()[0]) for k, v in (l.split(":", 1) for l in open("/proc/meminfo"))}
        avail, total = mem["MemAvailable"] / 1048576, mem["MemTotal"] / 1048576
        rows.append(_row("الرامات", WARN if avail < 1 else OK, f"{avail:.1f} من {total:.1f} جيجا فاضية"))
    except (OSError, KeyError, ValueError):
        pass
    du = shutil.disk_usage(config.DATA_DIR)
    free_gb = du.free / 1e9
    rows.append(_row("الهارد", ERR if free_gb < 3 else WARN if free_gb < 10 else OK, f"{free_gb:.0f} جيجا فاضية"))
    try:
        l1, l5, _ = os.getloadavg()
        rows.append(_row("الضغط على المعالج", OK, f"{l5:.2f} (آخر 5 دقايق) · {os.cpu_count()} أنوية"))
    except OSError:
        pass
    hb = db.kv_get("worker_heartbeat", 0)
    age = time.time() - hb if hb else None
    rows.append(_row("الماكينة (worker)", OK if age is not None and age < 120 else ERR,
                     f"آخر نبضة من {int(age)} ثانية" if age is not None else "مفيش نبض"))
    rows.append(_row("نسخة البرنامج", OK, __version__))
    return _section("السيرفر", rows)


def check_settings() -> dict:
    s = config.load_settings()
    g = s.get("google", {})
    rows = [
        _row("تطبيق جوجل (Client)", OK if g.get("client_id") and g.get("client_secret") else ERR),
        _row("مفتاح الصوت", OK if g.get("tts_api_key") else ERR),
        _row("درايف", OK if config.load_token("drive") else ERR, s.get("drive", {}).get("email", "")),
        _row("النشر العلني", OK if s.get("youtube_audited") else WARN,
             "علني ومجدول" if s.get("youtube_audited") else "الفيديوهات بتترفع Private"),
        _row("زرار الطوارئ", WARN if s.get("emergency_stop") else OK, "متفعّل: مفيش نشر" if s.get("emergency_stop") else "مقفول"),
    ]
    for cid, ch in s["channels"].items():
        if not ch.get("enabled"):
            continue
        yt = ch.get("youtube", {})
        rows.append(_row(f"قناة {ch['name']}", OK if yt.get("connected") else ERR,
                         yt.get("channel_title", "") or "يوتيوب مش مربوط"))
        rows.append(_row(f"النشر التلقائي · {ch['name']}", OK if ch.get("auto_publish") else WARN,
                         "متفعّل" if ch.get("auto_publish") else "كل فيديو مستني موافقتك"))
    return _section("الإعدادات والربط", rows)


def check_google(deep: bool) -> dict:
    rows = []
    if not deep or config.OFFLINE:
        return _section("خدمات جوجل", [_row("اتخطّى", OK, "فحص سريع")])
    s = config.load_settings()
    try:
        from .google import tts
        voice = next((c["voice"]["name"] for c in s["channels"].values() if c.get("enabled")), "en-US-Chirp3-HD-Charon")
        t0 = time.time()
        wav = tts.synthesize("Health check.", voice)
        rows.append(_row("صوت جوجل", OK if len(wav) > 1000 else ERR, f"اشتغل في {time.time() - t0:.1f} ث · {voice}"))
    except Exception as e:
        rows.append(_row("صوت جوجل", ERR, str(e)[:200]))
    try:
        from .google.drive import get_drive
        from .pipeline import ensure_drive_folders
        drive = get_drive()
        folders = ensure_drive_folders(drive, s)
        n = sum(len(drive.list_files(folders[f"{c}/packages"])) for c, ch in s["channels"].items() if ch.get("enabled"))
        rows.append(_row("درايف", OK, f"شايف {n} ملف سكربت"))
    except Exception as e:
        rows.append(_row("درايف", ERR, str(e)[:200]))
    from .google import youtube
    for cid, ch in s["channels"].items():
        if ch.get("enabled") and ch.get("youtube", {}).get("connected"):
            try:
                info = youtube.channel_info(cid)
                rows.append(_row(f"يوتيوب · {ch['name']}", OK, info.get("title", "")))
            except Exception as e:
                rows.append(_row(f"يوتيوب · {ch['name']}", ERR, str(e)[:200]))
    return _section("خدمات جوجل", rows)


def check_sources(deep: bool, links_per_source: int = 3) -> dict:
    from . import radar
    from .google.http import request
    s = config.load_settings()
    rows = []
    now = datetime.now(timezone.utc)
    for cid, ch in s["channels"].items():
        if not ch.get("enabled"):
            continue
        for src in ch.get("sources", []):
            if not src.get("enabled", True):
                continue
            name = f"{src['name']}"
            try:
                items = radar.fetch_source(src)
            except Exception as e:
                rows.append(_row(name, ERR, f"مش بيفتح: {str(e)[:150]}"))
                continue
            trends = src.get("role") == "trends"
            page = src["url"].split("?")[0].rstrip("/")
            fresh = [x for x in items if x.get("url") and x["url"].split("?")[0].rstrip("/") != page
                     and (radar.item_date(x) or now) > now - timedelta(days=2 if trends else radar.MAX_AGE_DAYS)]
            dates = [d for d in (radar.item_date(x) for x in items) if d]
            newest = max(dates) if dates else None
            newest_txt = newest.strftime("%b %d") if newest else "مفيش تواريخ"
            links_ok = links_bad = 0
            bad_examples = []
            if deep:
                for x in fresh[:links_per_source]:
                    try:
                        r = request("GET", x["url"], timeout=20, retries=0)
                        words = set(re.findall(r"[a-z]{5,}", x["title"].lower()))
                        body = r.text.lower() if r.status_code < 400 else ""
                        good = r.status_code < 400 and (trends or not words or
                                                         sum(w in body for w in words) >= max(1, len(words) // 2))
                        code = r.status_code
                    except Exception:
                        good, code = False, "ERR"
                    links_ok += good
                    links_bad += not good
                    if not good:
                        bad_examples.append(f"{code} {x['url'][:90]}")
            monthly = src["id"].startswith("bls")
            if not items:
                st = WARN
            elif links_bad:
                st = WARN if links_ok else ERR
            elif not fresh and not monthly and not trends:
                st = WARN
            else:
                st = OK
            detail = f"{len(items)} عنوان · {len(fresh)} جديد · أحدث خبر {newest_txt}"
            if deep:
                detail += f" · روابط سليمة {links_ok}/{links_ok + links_bad}"
            if monthly and not fresh:
                detail += " · (تقرير شهري، طبيعي)"
            if bad_examples:
                detail += " · " + " | ".join(bad_examples[:2])
            rows.append(_row(name, st, detail))
    return _section("مصادر الرادار", rows)


def check_queue() -> dict:
    rows = []
    c = db.conn()
    counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM packages GROUP BY status")}
    for st in ("needs_review", "approved", "rendering", "ready", "uploading", "failed"):
        n = counts.get(st, 0)
        level = ERR if st == "failed" and n else WARN if st == "needs_review" and n else OK
        rows.append(_row(db.STATUS_AR.get(st, st), level, str(n)))
    day = time.time() - 86400
    pub = c.execute("SELECT COUNT(*) n FROM packages WHERE status IN ('published','scheduled','private') AND updated_at>?",
                    (day,)).fetchone()["n"]
    rows.append(_row("اترفع آخر 24 ساعة", OK, str(pub)))
    errs = [e for e in db.events(300, "error") if e["ts"] > day]
    rows.append(_row("أخطاء آخر 24 ساعة", ERR if len(errs) >= 3 else WARN if errs else OK,
                     " | ".join(e["message"][:90] for e in errs[:3]) or "مفيش"))
    last_radar = db.kv_get("timer:radar", 0)
    age = time.time() - last_radar if last_radar else None
    rows.append(_row("آخر لفة رادار", OK if age is not None and age < 1800 else ERR,
                     f"من {int(age // 60)} دقيقة" if age is not None else "لسه"))
    return _section("الطابور والسجل", rows)


# ------------------------------------------------------------------ run / store
def run(deep: bool = True) -> dict:
    t0 = time.time()
    sections = []
    for fn in (check_system, check_settings, lambda: check_google(deep), lambda: check_sources(deep), check_queue):
        try:
            sections.append(fn())
        except Exception as e:
            sections.append(_section("فحص", [_row("خطأ في الفحص نفسه", ERR, str(e)[:200])]))
    errs = sum(r["status"] == ERR for sec in sections for r in sec["rows"])
    warns = sum(r["status"] == WARN for sec in sections for r in sec["rows"])
    report = {
        "generated": datetime.now(timezone.utc).isoformat(), "version": __version__, "deep": deep,
        "took_sec": round(time.time() - t0, 1), "errors": errs, "warnings": warns,
        "status": ERR if errs else WARN if warns else OK, "sections": sections,
    }
    report["text"] = to_text(report)
    return report


def to_text(rep: dict) -> str:
    icon = {OK: "✅", WARN: "⚠️", ERR: "❌"}
    lines = [f"فحص شامل · نسخة {rep['version']} · {rep['generated'][:16].replace('T', ' ')} UTC · "
             f"{rep['errors']} أخطاء · {rep['warnings']} تنبيهات"]
    for sec in rep["sections"]:
        lines.append(f"\n{icon[sec['status']]} {sec['title']}")
        for r in sec["rows"]:
            lines.append(f"  {icon[r['status']]} {r['name']}: {r['detail']}".rstrip(": "))
    return "\n".join(lines)


def request_run() -> None:
    db.kv_set("health_requested", time.time())


def maybe_run() -> bool:
    """Called from the worker: run if the owner pressed the button, or once a day. Returns True if it ran."""
    req = db.kv_get("health_requested", 0)
    last = (db.kv_get("health_report") or {}).get("_ts", 0)
    due_daily = time.time() - last > 86400
    if not req and not due_daily:
        return False
    db.kv_set("health_requested", 0)
    db.kv_set("health_running", time.time())
    try:
        rep = run(deep=True)
        rep["_ts"] = time.time()
        db.kv_set("health_report", rep)
        try:
            from .google.drive import get_drive
            from .pipeline import ensure_drive_folders
            if config.OFFLINE or config.load_token("drive"):
                s = config.load_settings()
                drive = get_drive()
                drive.write_json(ensure_drive_folders(drive, s)["_system"], "health_report.json", rep)
        except Exception:
            pass
    finally:
        db.kv_set("health_running", 0)
    return True
