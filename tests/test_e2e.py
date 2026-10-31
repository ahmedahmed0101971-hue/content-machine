"""Full path, offline: Claude drops a package in Drive -> worker imports, checks, auto-approves,
renders with voice, uploads (mock) -> dashboard shows it."""
import json
import subprocess

from conftest import load_sample

from machine import config, db, pipeline, worker
from machine.google.drive import LocalDrive


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                          "-of", "json", path], capture_output=True, text=True).stdout
    return json.loads(out)


def test_full_flow_from_drive(clean_db):
    config.update_settings(lambda s: (s["channels"]["money"].update({"auto_publish": True}),
                                      s["channels"]["money"]["youtube"].update({"connected": True}),
                                      s.update({"youtube_audited": False})))
    drive = LocalDrive()
    folders = drive.ensure_structure(["money", "recalls", "sky"])
    pkg = load_sample("sample-short-cola")
    pkg["id"] = "e2e-short-cola"
    drive.write_json(folders["money/packages"], "e2e-short-cola.json", pkg)
    drive.write_json(folders["money/packages"], "garbage.json", {"hello": 1})

    worker.tick()                                   # import + check
    p = db.pkg_get("e2e-short-cola")
    assert p is not None and p["status"] == "approved", p and (p["status"], p["check_result"])
    assert any("مشاكل" in e["message"] for e in db.events(50))   # the garbage file was reported

    assert pipeline.render_next()
    p = db.pkg_get("e2e-short-cola")
    assert p["status"] == "ready", p["error"]
    info = _probe(p["video_path"])
    kinds = {s["codec_type"] for s in info["streams"]}
    assert kinds == {"video", "audio"}
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1080, 1920)

    assert pipeline.upload_next()
    p = db.pkg_get("e2e-short-cola")
    assert p["status"] == "private" and p["youtube_id"].startswith("OFF")
    rec = [json.loads(l) for l in (config.DATA_DIR / "offline_uploads.jsonl").read_text().splitlines()]
    body = rec[-1]["body"]
    assert body["status"]["privacyStatus"] == "private" and "publishAt" not in body["status"]
    assert body["status"]["containsSyntheticMedia"] is True

    # snapshot for Claude is on Drive
    pipeline.sync_to_drive()
    snap = json.loads((config.DATA_DIR / "local_drive" / "ContentMachine" / "_system" / "system_snapshot.json").read_text())
    assert any(r["id"] == "e2e-short-cola" for r in snap["channels"]["money"]["recent"])
    assert "password_hash" not in json.dumps(snap) and "client_secret" not in json.dumps(snap)

    # re-running the import does not duplicate
    db.kv_set("timer:import", 0)
    worker.tick()
    assert len(db.pkg_list(channel="money")) == 1


def test_scheduled_upload_when_audited(clean_db):
    config.update_settings(lambda s: (s["channels"]["money"]["youtube"].update({"connected": True}),
                                      s.update({"youtube_audited": True})))
    try:
        pkg = load_sample("sample-short-cola")
        pkg["id"] = "e2e-scheduled"
        pkg["date"] = "2099-01-01"
        db.pkg_insert(pkg, "approved")
        db.pkg_update("e2e-scheduled", publish_at="2099-01-01T12:00:00+00:00")
        assert pipeline.render_next()
        assert pipeline.upload_next()
        p = db.pkg_get("e2e-scheduled")
        assert p["status"] == "scheduled"
        body = json.loads((config.DATA_DIR / "offline_uploads.jsonl").read_text().splitlines()[-1])["body"]
        assert body["status"]["publishAt"] == "2099-01-01T12:00:00Z"
    finally:
        config.update_settings(lambda s: s.update({"youtube_audited": False}))


def test_long_video_renders_with_thumbnail(clean_db):
    pkg = load_sample("sample-long-scam")
    pkg["id"] = "e2e-long"
    db.pkg_insert(pkg, "approved")
    assert pipeline.render_next()
    p = db.pkg_get("e2e-long")
    assert p["status"] == "ready", p["error"]
    info = _probe(p["video_path"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1920, 1080)
    assert p["thumb_path"].endswith("thumbnail.jpg")


def test_emergency_stop_blocks_upload(clean_db):
    config.update_settings(lambda s: (s.update({"emergency_stop": True}), s["channels"]["money"]["youtube"].update({"connected": True})))
    try:
        pkg = load_sample("sample-short-cola")
        pkg["id"] = "e2e-stop"
        db.pkg_insert(pkg, "ready")
        db.pkg_update("e2e-stop", video_path="/nonexistent.mp4")
        assert not pipeline.upload_next()
        assert db.pkg_get("e2e-stop")["status"] == "ready"
    finally:
        config.update_settings(lambda s: s.update({"emergency_stop": False}))


def test_rewrite_after_rejection_replaces_package(clean_db):
    drive = LocalDrive()
    folders = drive.ensure_structure(["money", "recalls", "sky"])
    pkg = load_sample("sample-short-cola")
    pkg["id"] = "rewrite-test"
    drive.write_json(folders["money/packages"], "rewrite-test", pkg)       # no .json extension on purpose
    pipeline.import_from_drive()
    db.pkg_update("rewrite-test", status="rejected", note="better hook please")
    pkg["scenes"][0]["vo"] = "Big news for anyone on Social Security."
    import os, time as _t
    path = os.path.join(folders["money/packages"], "rewrite-test")
    drive.write_json(folders["money/packages"], "rewrite-test", pkg)
    os.utime(path, (_t.time() + 5, _t.time() + 5))
    pipeline.import_from_drive()
    p = db.pkg_get("rewrite-test")
    assert p["status"] == "new" and p["data"]["scenes"][0]["vo"].startswith("Big news")


def test_two_render_threads_in_parallel(clean_db):
    import threading
    for k in range(2):
        pkg = load_sample("sample-short-cola")
        pkg["id"] = f"parallel-{k}"
        db.pkg_insert(pkg, "approved")
    ts = [threading.Thread(target=pipeline.render_next) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert {db.pkg_get(f"parallel-{k}")["status"] for k in range(2)} == {"ready"}
