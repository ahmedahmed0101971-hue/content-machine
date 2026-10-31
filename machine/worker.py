"""Background worker: one process, a few threads. Started by systemd (content-machine-worker.service)."""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

from . import config, db, events, pipeline, radar
from .package import ET

log = logging.getLogger("machine.worker")
RENDER_THREADS = max(1, int(os.environ.get("MACHINE_RENDER_THREADS", "1")))
_stop = threading.Event()


def _throttled_error(key: str, msg: str, channel: str | None = None) -> None:
    last = db.kv_get(f"errthrottle:{key}", 0)
    if time.time() - last > 3600:
        db.kv_set(f"errthrottle:{key}", time.time())
        events.error(msg, channel)
    else:
        log.error(msg)


def _due(name: str, every_sec: int) -> bool:
    last = db.kv_get(f"timer:{name}", 0)
    if time.time() - last >= every_sec:
        db.kv_set(f"timer:{name}", time.time())
        return True
    return False


def drive_ready() -> bool:
    return config.OFFLINE or bool(config.load_token("drive"))


def tick() -> None:
    """One pass of the scheduler loop (also called by tests)."""
    s = config.load_settings()
    db.kv_set("worker_heartbeat", time.time())

    if drive_ready() and _due("import", 120):
        try:
            pipeline.import_from_drive()
        except Exception as e:
            _throttled_error("import", f"مش قادر أقرأ الباكدجات من درايف: {e}")

    try:
        pipeline.check_new()
    except Exception as e:
        _throttled_error("check", f"مشكلة في فحص الباكدجات: {e}")

    try:
        radar.check_awaiting()
    except Exception as e:
        _throttled_error("awaiting", f"مشكلة في متابعة الأرقام المنتظرة: {e}")

    if _due("radar", 900):
        for cid, ch in s["channels"].items():
            if not ch.get("enabled"):
                continue
            try:
                new = radar.run_channel(cid, ch)
                for it in new:
                    if it["tier"] == 2:
                        tag = " · عليه تريند" if it.get("trend_match") else ""
                        events.info(f"📡 خبر مهم من {it['source']}{tag}: {it['title'][:140]}", cid, notify=True)
            except Exception as e:
                _throttled_error(f"radar:{cid}", f"الرادار وقف لقناة {cid}: {e}", cid)
        if drive_ready():
            try:
                pipeline.sync_to_drive(include_candidates=True)
            except Exception as e:
                _throttled_error("sync", f"مش قادر أكتب على درايف: {e}")
    elif drive_ready() and _due("snapshot", 600):
        try:
            pipeline.sync_to_drive(include_candidates=False)
        except Exception as e:
            _throttled_error("sync", f"مش قادر أكتب على درايف: {e}")

    now_et = datetime.now(ET)
    week_key = now_et.strftime("%G-W%V")
    if drive_ready() and now_et.weekday() == 6 and db.kv_get("analytics_week") != week_key:
        db.kv_set("analytics_week", week_key)
        pipeline.sync_analytics()

    if _due("cleanup", 86400):
        pipeline.cleanup()


def _render_loop() -> None:
    while not _stop.is_set():
        try:
            did = pipeline.render_next()
        except Exception as e:
            _throttled_error("render", f"مشكلة في التجهيز: {e}")
            did = False
        if not did:
            _stop.wait(10)


def _upload_loop() -> None:
    while not _stop.is_set():
        try:
            did = pipeline.upload_next()
        except Exception as e:
            _throttled_error("upload", f"مشكلة في الرفع: {e}")
            did = False
        if not did:
            _stop.wait(10)


def recover() -> None:
    """After a restart, put interrupted jobs back in the queue."""
    for p in db.pkg_list(status="rendering"):
        db.pkg_update(p["id"], status="approved")
    for p in db.pkg_list(status="uploading"):
        db.pkg_update(p["id"], status="ready")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config.ensure_dirs()
    recover()
    events.info("الماكينة اشتغلت ✅")
    threads = [threading.Thread(target=_render_loop, daemon=True, name=f"render{i}") for i in range(RENDER_THREADS)]
    threads.append(threading.Thread(target=_upload_loop, daemon=True, name="upload"))
    for t in threads:
        t.start()
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:
            _throttled_error("tick", f"خطأ عام في الماكينة: {e}")
        _stop.wait(30)


if __name__ == "__main__":
    main()
