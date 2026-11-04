"""Arabic dashboard (Flask). Runs on 127.0.0.1:8080 behind Tailscale (or any reverse proxy)."""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import re
import time
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, redirect, render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .. import __version__, config, db, events, pipeline
from ..google import oauth, tts, youtube
from ..google.drive import Drive
from ..google.http import GoogleError

CAIRO = ZoneInfo("Africa/Cairo")
ET = ZoneInfo("America/New_York")
NAV = [("review", "المراجعة"), ("radar_page", "الرادار"), ("settings_page", "الإعدادات"), ("sources_page", "المصادر"), ("log_page", "السجل"), ("niche_page", "باحث النيشات"), ("yt_page", "تحليل قناة"), ("health_page", "الفحص")]


def create_app() -> Flask:
    config.ensure_dirs()
    s = config.load_settings()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = s["secret_key"]
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=5 * 1024 * 1024,
                      PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

    # ---------------------------------------------------------------- helpers
    @app.template_filter("cairo")
    def f_cairo(iso_or_ts):
        if not iso_or_ts:
            return "—"
        dt = datetime.fromtimestamp(iso_or_ts, timezone.utc) if isinstance(iso_or_ts, (int, float)) else datetime.fromisoformat(iso_or_ts)
        return "\u2066" + dt.astimezone(CAIRO).strftime("%a %d/%m · %I:%M %p") + "\u2069"

    @app.template_filter("et")
    def f_et(iso):
        if not iso:
            return "—"
        return "\u2066" + datetime.fromisoformat(iso).astimezone(ET).strftime("%I:%M %p ET") + "\u2069"

    @app.template_filter("pubdate")
    def f_pubdate(v):
        from ..radar import parse_date
        dt = parse_date(v or "")
        return "\u2066" + dt.astimezone(ET).strftime("%b %d") + "\u2069" if dt else "—"

    @app.template_filter("ago")
    def f_ago(ts):
        if not ts:
            return "أبداً"
        d = int(time.time() - ts)
        if d < 90:
            return "من شوية"
        if d < 3600:
            return f"من {d // 60} دقيقة"
        if d < 86400:
            return f"من {d // 3600} ساعة"
        return f"من {d // 86400} يوم"

    @app.context_processor
    def inject():
        st = config.load_settings()
        return {"NAV": NAV, "S": st, "STATUS_AR": db.STATUS_AR, "VERSION": __version__,
                "csrf": session.get("csrf", ""), "heartbeat": db.kv_get("worker_heartbeat", 0),
                "now": time.time(), "OFFLINE": config.OFFLINE}

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            st = config.load_settings()
            if not st.get("password_hash"):
                return redirect(url_for("welcome"))
            if not session.get("ok"):
                return redirect(url_for("login", next=request.path))
            return fn(*a, **kw)
        return wrapper

    @app.before_request
    def csrf_protect():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST" and request.endpoint not in ("login", "welcome"):
            if request.form.get("csrf") != session.get("csrf"):
                abort(400)

    def back(default="review"):
        return redirect(request.form.get("next") or request.referrer or url_for(default))

    # ---------------------------------------------------------------- auth
    @app.route("/welcome", methods=["GET", "POST"])
    def welcome():
        st = config.load_settings()
        if st.get("password_hash"):
            return redirect(url_for("login"))
        if request.method == "POST":
            pw, pw2 = request.form.get("pw", ""), request.form.get("pw2", "")
            if len(pw) < 8:
                flash("كلمة السر لازم تكون 8 حروف على الأقل", "err")
            elif pw != pw2:
                flash("كلمتين السر مش زي بعض", "err")
            else:
                config.update_settings(lambda x: x.update({"password_hash": generate_password_hash(pw)}))
                session["ok"] = True
                session.permanent = True
                return redirect(url_for("setup"))
        return render_template("welcome.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            fails = db.kv_get("login_fails", [])
            fails = [t for t in fails if time.time() - t < 900]
            if len(fails) >= 8:
                flash("محاولات كتير غلط. استنى ربع ساعة.", "err")
                return render_template("login.html")
            if check_password_hash(config.load_settings()["password_hash"], request.form.get("pw", "")):
                session["ok"] = True
                session.permanent = True
                db.kv_set("login_fails", [])
                nxt = request.args.get("next", "/")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            fails.append(time.time())
            db.kv_set("login_fails", fails)
            flash("كلمة السر غلط", "err")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------------------------------------------------------------- review
    @app.route("/")
    @login_required
    def review():
        ch = request.args.get("ch") or ""
        groups = {
            "review": db.pkg_list(status="needs_review", channel=ch or None),
            "working": db.pkg_list(status=("new", "approved", "rendering", "uploading", "awaiting"), channel=ch or None),
            "ready": db.pkg_list(status="ready", channel=ch or None),
            "done": db.pkg_list(status=("scheduled", "published", "private"), channel=ch or None, limit=12),
            "failed": db.pkg_list(status="failed", channel=ch or None, limit=10),
        }
        return render_template("review.html", g=groups, ch=ch)

    @app.route("/pkg/<pid>")
    @login_required
    def pkg_detail(pid):
        p = db.pkg_get(pid) or abort(404)
        return render_template("pkg.html", p=p)

    @app.post("/pkg/<pid>/<action>")
    @login_required
    def pkg_action(pid, action):
        p = db.pkg_get(pid) or abort(404)
        title = p["data"]["youtube"]["title"]
        note = request.form.get("note", "").strip()[:500]
        if action == "approve" and p["status"] in ("needs_review", "rejected"):
            db.pkg_update(pid, status="approved", note=note)
            events.info(f"اعتمدت: {title}", p["channel"])
            flash("اتعتمد ✅ ودخل طابور التجهيز", "ok")
        elif action == "reject":
            db.pkg_update(pid, status="rejected", note=note or "مرفوض بدون ملاحظة")
            events.info(f"رفضت: {title} · {note}", p["channel"])
            flash("اترفض. ملاحظتك هتوصل لـClaude في التشغيلة الجاية.", "ok")
        elif action == "retry" and p["status"] in ("failed",):
            db.pkg_update(pid, status="approved", attempts=0, error="")
            flash("رجع للطابور", "ok")
        elif action == "upload_now" and p["status"] == "ready":
            db.pkg_update(pid, publish_at=datetime.now(timezone.utc).isoformat())
            flash("هيترفع في خلال دقيقة", "ok")
        elif action == "mark_uploaded" and p["status"] == "ready":
            db.pkg_update(pid, status="published", note="اترفع يدوي")
            flash("اتسجّل إنه اترفع يدوي", "ok")
        elif action == "delete":
            d = config.WORK_DIR / pid
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            db.conn().execute("DELETE FROM packages WHERE id=?", (pid,))
            flash("اتمسح", "ok")
            return redirect(url_for("review"))
        else:
            flash("الإجراء ده مش متاح دلوقتي", "err")
        return back()

    @app.route("/media/<pid>/<kind>")
    @login_required
    def media(pid, kind):
        p = db.pkg_get(pid) or abort(404)
        path = p["video_path"] if kind == "video" else p["thumb_path"] if kind == "thumb" else None
        if kind == "srt":
            path = str(config.WORK_DIR / pid / "captions.srt")
        if not path or not os.path.exists(path):
            abort(404)
        dl = request.args.get("dl") == "1"
        name = f"{pid}.{ 'mp4' if kind == 'video' else 'jpg' if kind == 'thumb' else 'srt'}"
        return send_file(path, as_attachment=dl, download_name=name, conditional=True)

    @app.post("/upload-package")
    @login_required
    def upload_package():
        f = request.files.get("file")
        if not f:
            flash("اختار ملف", "err")
            return back()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            f.save(tmp.name)
        try:
            pid = pipeline.import_local(tmp.name)
            flash(f"اتضاف: {pid}", "ok")
        except Exception as e:
            flash(f"الملف فيه مشكلة: {e}", "err")
        finally:
            os.unlink(tmp.name)
        return back()

    # ---------------------------------------------------------------- radar
    @app.route("/radar")
    @login_required
    def radar_page():
        ch = request.args.get("ch") or ""
        trends = [dict(r) for r in db.conn().execute(
            "SELECT * FROM radar_items WHERE tier=0" + (" AND channel=?" if ch else "") +
            " ORDER BY first_seen DESC LIMIT 15", ([ch] if ch else [])).fetchall()]
        q = "SELECT * FROM radar_items WHERE tier IN (1,2,3)"
        args: list = []
        if ch:
            q += " AND channel=?"
            args.append(ch)
        q += " ORDER BY COALESCE(NULLIF(REPLACE(SUBSTR(published,1,19),'T',' '),''), datetime(first_seen,'unixepoch')) DESC LIMIT 150"
        items = [dict(r) for r in db.conn().execute(q, args).fetchall()]
        return render_template("radar.html", items=items, trends=trends, ch=ch, awaiting=db.pkg_list(status="awaiting"))

    # ---------------------------------------------------------------- settings
    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        if request.method == "POST":
            form = request.form
            section = form.get("section")

            def apply(st):
                if section == "global":
                    st["emergency_stop"] = form.get("emergency_stop") == "on"
                    st["youtube_audited"] = form.get("youtube_audited") == "on"
                    st["telegram"]["bot_token"] = form.get("tg_token", "").strip()
                    st["telegram"]["chat_id"] = form.get("tg_chat", "").strip()
                elif section in st["channels"]:
                    c = st["channels"][section]
                    c["enabled"] = form.get("enabled") == "on"
                    c["auto_publish"] = form.get("auto_publish") == "on"
                    c["voice"]["name"] = form.get("voice", c["voice"]["name"])
                    try:
                        c["voice"]["rate"] = max(0.75, min(1.3, float(form.get("rate", 1.0))))
                    except ValueError:
                        pass
                    c["theme"]["brand"] = form.get("brand", c["theme"]["brand"]).strip()[:40] or c["theme"]["brand"]
                    for slot in list(c["slots"].keys()):
                        v = form.get(f"slot_{slot}", "").strip()
                        if len(v) == 5 and v[2] == ":":
                            c["slots"][slot] = v
                    for k in ("max_long", "max_short"):
                        try:
                            c["limits"][k] = max(0, min(10, int(form.get(k, c["limits"].get(k, 3)))))
                        except ValueError:
                            pass
            config.update_settings(apply)
            if section == "global" and form.get("emergency_stop") == "on":
                events.warn("🛑 إيقاف الطوارئ اتفعّل: مفيش رفع لحد ما تقفله")
            flash("اتحفظ ✅", "ok")
            return redirect(url_for("settings_page") + (f"#{section}" if section else ""))
        return render_template("settings.html", voices=tts.VOICES)

    @app.post("/test-voice")
    @login_required
    def test_voice():
        voice = request.form.get("voice", "en-US-Chirp3-HD-Charon")
        rate = float(request.form.get("rate", 1.0))
        try:
            wav = tts.synthesize("Your November Social Security payment dates are here. Here is exactly when your money arrives.", voice, rate)
        except Exception as e:
            flash(f"الصوت ماشتغلش: {e}", "err")
            return back("settings_page")
        p = config.WORK_DIR / "voice_test.wav"
        p.write_bytes(wav)
        return send_file(str(p), mimetype="audio/wav")

    # ---------------------------------------------------------------- sources
    @app.route("/sources", methods=["GET", "POST"])
    @login_required
    def sources_page():
        if request.method == "POST":
            cid = request.form.get("channel")

            def apply(st):
                c = st["channels"][cid]
                for src in c["sources"]:
                    src["enabled"] = request.form.get(f"src_{src['id']}") == "on"
                c["blocked_words"] = [w.strip() for w in request.form.get("blocked", "").split(",") if w.strip()]
                c["priority_keywords"] = [w.strip() for w in request.form.get("priority", "").split(",") if w.strip()]
                c["allowed_phrases"] = [w.strip() for w in request.form.get("allowed", "").split(",") if w.strip()]
            config.update_settings(apply)
            flash("اتحفظ ✅", "ok")
            return redirect(url_for("sources_page") + f"#{cid}")
        status = {}
        for r in db.conn().execute("SELECT * FROM source_status").fetchall():
            status[(r["channel"], r["source_id"])] = dict(r)
        return render_template("sources.html", status=status)

    # ---------------------------------------------------------------- log
    # ---------------------------------------------------------------- channel analyzer
    @app.route("/niche", methods=["GET", "POST"])
    @login_required
    def niche_page():
        from .. import niche
        if request.method == "POST":
            f = request.form
            if f.get("cancel"):
                niche.cancel(f["cancel"])
                flash("اتشال من الطابور", "ok")
                return redirect(url_for("niche_page"))
            try:
                ids = niche.new_batch(f.get("queries", ""), sort=f.get("sort", "relevance"), period=f.get("period", "any"),
                                      per_query=int(f.get("per_query") or 100), new_months=int(f.get("new_months") or 12),
                                      deep=int(f.get("deep") or 10))
                if len(ids) == 1:
                    flash("البحث بدأ. الصفحة بتتحدّث لوحدها.", "ok")
                    return redirect(url_for("niche_report", jid=ids[0]))
                flash(f"اتضاف {len(ids)} نيش للطابور. هيتعملوا ورا بعض لوحدهم، وتقدر تقفل الصفحة.", "ok")
            except ValueError as e:
                flash(str(e), "err")
            return redirect(url_for("niche_page"))
        jobs = niche.list_jobs()
        for j in jobs:
            j["pos"] = niche.queue_position(j["id"])
        ranked = sorted([j for j in jobs if j["status"] == "done" and j.get("score") is not None],
                        key=lambda j: j["score"], reverse=True)
        return render_template("niche.html", jobs=jobs, ranked=ranked, SORT_AR=niche.SORT_AR, PERIOD_AR=niche.PERIOD_AR)

    @app.route("/niche/<jid>")
    @login_required
    def niche_report(jid):
        from .. import niche
        job = niche.get_job(jid) or abort(404)
        rep = niche.load_report(jid) if job["status"] == "done" else None
        return render_template("niche_report.html", job=job, rep=rep)

    @app.route("/niche/<jid>/download/<fmt>")
    @login_required
    def niche_download(jid, fmt):
        from .. import niche
        if not re.fullmatch(r"[\w-]+", jid) or fmt not in ("xlsx", "json"):
            abort(404)
        job = niche.get_job(jid) or abort(404)
        p = niche.reports_dir() / f"{jid}.{fmt}"
        if not p.exists():
            abort(404)
        name = re.sub(r"[^\w-]+", "-", job["queries"][0])[:40] or "niche"
        return send_file(str(p), as_attachment=True, download_name=f"niche-{name}-{jid}.{fmt}")

    @app.post("/niche/<jid>/delete")
    @login_required
    def niche_delete(jid):
        from .. import niche
        db.kv_set("nichejobs", [i for i in db.kv_get("nichejobs", []) if i != jid])
        for ext in ("json", "xlsx"):
            (niche.reports_dir() / f"{jid}.{ext}").unlink(missing_ok=True)
        flash("اتمسح", "ok")
        return redirect(url_for("niche_page"))

    @app.route("/yt", methods=["GET", "POST"])
    @login_required
    def yt_page():
        from .. import ytanalyze
        if request.method == "POST":
            try:
                tabs = [t for t in ("videos", "shorts", "streams") if request.form.get(f"tab_{t}") == "on"]
                jid = ytanalyze.new_job(request.form.get("url", ""), tabs, request.form.get("mode", "fast"),
                                        int(request.form.get("limit") or ytanalyze.MAX_VIDEOS))
                flash("التحليل بدأ. الصفحة بتتحدّث لوحدها.", "ok")
                return redirect(url_for("yt_report", jid=jid))
            except ValueError as e:
                flash(str(e), "err")
                return redirect(url_for("yt_page"))
        return render_template("yt.html", jobs=ytanalyze.list_jobs(), tabs=ytanalyze.TABS, maxv=ytanalyze.MAX_VIDEOS,
                               prefill=request.args.get("url", ""))

    @app.route("/yt/<jid>")
    @login_required
    def yt_report(jid):
        from .. import ytanalyze
        job = ytanalyze.get_job(jid) or abort(404)
        rep = ytanalyze.load_report(jid) if job["status"] == "done" else None
        if rep:
            rep["videos"].sort(key=lambda r: r.get("views") or 0, reverse=True)
        return render_template("yt_report.html", job=job, rep=rep)

    @app.route("/yt/<jid>/download/<fmt>")
    @login_required
    def yt_download(jid, fmt):
        from .. import ytanalyze
        if not re.fullmatch(r"[\w-]+", jid) or fmt not in ("xlsx", "json", "csv"):
            abort(404)
        job = ytanalyze.get_job(jid) or abort(404)
        name = re.sub(r"[^\w-]+", "-", job.get("channel_title") or "channel")[:40] or "channel"
        if fmt == "csv":
            import csv, io
            rep = ytanalyze.load_report(jid) or abort(404)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([h for _, h in ytanalyze.COLS])
            for r in sorted(rep["videos"], key=lambda r: r.get("views") or 0, reverse=True):
                w.writerow([", ".join(map(str, r.get(k))) if isinstance(r.get(k), list) else r.get(k) for k, _ in ytanalyze.COLS])
            p = ytanalyze.REPORTS() / f"{jid}.csv"
            p.write_text("\ufeff" + buf.getvalue(), encoding="utf-8")
        else:
            p = ytanalyze.REPORTS() / f"{jid}.{fmt}"
        if not p.exists():
            abort(404)
        return send_file(str(p), as_attachment=True, download_name=f"{name}-{jid}.{fmt}")

    @app.post("/yt/<jid>/delete")
    @login_required
    def yt_delete(jid):
        from .. import ytanalyze
        db.kv_set("ytjobs", [i for i in db.kv_get("ytjobs", []) if i != jid])
        for ext in ("json", "xlsx", "csv"):
            (ytanalyze.REPORTS() / f"{jid}.{ext}").unlink(missing_ok=True)
        flash("اتمسح", "ok")
        return redirect(url_for("yt_page"))

    @app.route("/health", methods=["GET", "POST"])
    @login_required
    def health_page():
        from .. import health
        if request.method == "POST":
            health.request_run()
            flash("الفحص بدأ. بياخد من دقيقة لـ3 دقايق، والصفحة هتتحدّث لوحدها.", "ok")
            return redirect(url_for("health_page"))
        rep = db.kv_get("health_report")
        waiting = bool(db.kv_get("health_requested", 0) or db.kv_get("health_running", 0))
        worker_alive = time.time() - db.kv_get("worker_heartbeat", 0) < 120
        return render_template("health.html", rep=rep, waiting=waiting, worker_alive=worker_alive)

    @app.route("/log")
    @login_required
    def log_page():
        level = request.args.get("level") or None
        hist = db.pkg_list(status=("scheduled", "published", "private", "rejected", "failed"), limit=80)
        analytics = {cid: db.kv_get(f"analytics:{cid}") for cid in config.load_settings()["channels"]}
        return render_template("log.html", events=db.events(200, level), hist=hist, level=level or "", analytics=analytics)

    # ---------------------------------------------------------------- setup
    @app.route("/setup", methods=["GET", "POST"])
    @login_required
    def setup():
        if request.method == "POST":
            step = request.form.get("step")
            if step == "client":
                cid_ = request.form.get("client_id", "").strip()
                sec_ = request.form.get("client_secret", "").strip()
                config.update_settings(lambda x: x["google"].update({"client_id": cid_, "client_secret": sec_}))
                flash("بيانات تطبيق جوجل اتحفظت ✅", "ok")
            elif step == "tts":
                key = request.form.get("tts_key", "").strip()
                config.update_settings(lambda x: x["google"].update({"tts_api_key": key}))
                try:
                    tts.synthesize("Testing the voice.", "en-US-Chirp3-HD-Charon")
                    flash("مفتاح الصوت شغال ✅", "ok")
                except Exception as e:
                    flash(f"المفتاح اتحفظ بس التجربة فشلت: {e}", "err")
            elif step == "drive_start":
                try:
                    session["oauth_url"] = oauth.start("drive", "drive")
                    session["oauth_for"] = "drive"
                except GoogleError as e:
                    flash(str(e), "err")
            elif step.startswith("yt_start:"):
                cid = step.split(":", 1)[1]
                try:
                    session["oauth_url"] = oauth.start("youtube", youtube.token_name(cid))
                    session["oauth_for"] = cid
                except GoogleError as e:
                    flash(str(e), "err")
            elif step == "oauth_finish":
                try:
                    name, _ = oauth.finish(request.form.get("pasted", ""))
                    if name == "drive":
                        email = Drive().whoami()
                        folders = Drive().ensure_structure(list(config.load_settings()["channels"].keys()))
                        config.update_settings(lambda x: x["drive"].update(
                            {"connected": True, "email": email, "folders": folders, "root_id": folders["root"]}))
                        flash(f"درايف اتربط ✅ ({email}) واتعمل فولدر ContentMachine", "ok")
                    else:
                        cid = name.replace("youtube_", "", 1)
                        info = youtube.channel_info(cid)
                        config.update_settings(lambda x: x["channels"][cid]["youtube"].update(
                            {"connected": True, "channel_title": info["title"], "channel_id": info["id"]}))
                        flash(f"قناة '{info['title']}' اتربطت ✅", "ok")
                    session.pop("oauth_url", None)
                except Exception as e:
                    flash(f"الربط ماكملش: {e}", "err")
            elif step.startswith("yt_disconnect:"):
                cid = step.split(":", 1)[1]
                config.delete_token(youtube.token_name(cid))
                config.update_settings(lambda x: x["channels"][cid]["youtube"].update({"connected": False}))
                flash("القناة اتفصلت", "ok")
            elif step == "password":
                pw = request.form.get("pw", "")
                if len(pw) >= 8:
                    config.update_settings(lambda x: x.update({"password_hash": generate_password_hash(pw)}))
                    flash("كلمة السر اتغيرت ✅", "ok")
                else:
                    flash("8 حروف على الأقل", "err")
            return redirect(url_for("setup"))
        return render_template("setup.html", checks=system_checks(), oauth_url=session.get("oauth_url"),
                               oauth_for=session.get("oauth_for"))

    @app.post("/update")
    @login_required
    def update():
        script = config.ROOT_DIR / "update.sh"

        def run():
            r = subprocess.run(["sudo", "-n", str(script)], capture_output=True, text=True, timeout=900)
            (events.info if r.returncode == 0 else events.error)(
                "التحديث خلص ✅" if r.returncode == 0 else f"التحديث فشل: {r.stderr[-300:]}")
        threading.Thread(target=run, daemon=True).start()
        flash("التحديث بدأ. هيخلص في دقيقتين واللوحة هتفصل لحظة.", "ok")
        return back("setup")

    @app.errorhandler(404)
    def nf(_):
        return render_template("message.html", title="مش موجود", msg="الصفحة أو الفيديو ده مش موجود."), 404

    return app


def system_checks() -> list[tuple[str, bool, str]]:
    out = []
    out.append(("ffmpeg (الرندر)", shutil.which("ffmpeg") is not None, "موجود" if shutil.which("ffmpeg") else "مش متثبت"))
    out.append(("pdftotext (قراءة ملفات PDF الرسمية)", shutil.which("pdftotext") is not None, ""))
    fonts_ok = all((config.FONTS_DIR / f).exists() for f in ("InterDisplay-Black.otf", "Inter-Bold.otf"))
    out.append(("خطوط الفيديو", fonts_ok, ""))
    du = shutil.disk_usage(config.DATA_DIR)
    free_gb = du.free / 1e9
    out.append(("المساحة الفاضية", free_gb > 5, f"{free_gb:.0f} جيجا"))
    hb = db.kv_get("worker_heartbeat", 0)
    out.append(("الماكينة (Worker) شغالة", time.time() - hb < 180, "آخر نبضة " + (f"من {int(time.time() - hb)} ثانية" if hb else "مفيش")))
    st = config.load_settings()
    out.append(("بيانات تطبيق جوجل", bool(st["google"]["client_id"] and st["google"]["client_secret"]), ""))
    out.append(("مفتاح الصوت", bool(st["google"]["tts_api_key"]), ""))
    out.append(("جوجل درايف", bool(config.load_token("drive")), st["drive"].get("email", "")))
    for cid, ch in st["channels"].items():
        if ch.get("enabled"):
            out.append((f"يوتيوب · {ch['name']}", ch["youtube"].get("connected", False), ch["youtube"].get("channel_title", "")))
    return out


def main() -> None:
    app = create_app()
    host = os.environ.get("DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("DASH_PORT", "8080"))
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port)


if __name__ == "__main__":
    main()
