import io
import json

import pytest
from conftest import SAMPLES, load_sample

from machine import config, db


@pytest.fixture
def client(clean_db):
    config.update_settings(lambda s: s.update({"password_hash": ""}))
    from machine.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.post("/welcome", data={"pw": "a-strong-pass", "pw2": "a-strong-pass"})
    assert r.status_code == 302
    with c.session_transaction() as s:
        c.csrf = s["csrf"]
    return c


def test_first_run_and_login(clean_db):
    config.update_settings(lambda s: s.update({"password_hash": ""}))
    from machine.web.app import create_app
    c = create_app().test_client()
    assert c.get("/").location.endswith("/welcome")
    r = c.post("/welcome", data={"pw": "short", "pw2": "short"})
    assert "8 حروف" in r.get_data(as_text=True)
    c.post("/welcome", data={"pw": "a-strong-pass", "pw2": "a-strong-pass"})
    c.get("/logout")
    assert "/login" in c.get("/settings").location
    assert c.post("/login", data={"pw": "nope"}).status_code == 200
    assert c.post("/login", data={"pw": "a-strong-pass"}).status_code == 302


def test_all_pages_render(client):
    pkg = load_sample("sample-long-ssa-november")
    db.pkg_insert(pkg, "needs_review")
    for path in ("/", "/radar", "/settings", "/sources", "/log", "/setup", f"/pkg/{pkg['id']}"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/pkg/nothing-here").status_code == 404


def test_csrf_required(client):
    assert client.post("/settings", data={"section": "global"}).status_code == 400


def test_approve_and_reject(client):
    pkg = load_sample("sample-short-cola")
    db.pkg_insert(pkg, "needs_review")
    client.post(f"/pkg/{pkg['id']}/reject", data={"csrf": client.csrf, "note": "hook is weak"})
    p = db.pkg_get(pkg["id"])
    assert p["status"] == "rejected" and p["note"] == "hook is weak"
    client.post(f"/pkg/{pkg['id']}/approve", data={"csrf": client.csrf})
    assert db.pkg_get(pkg["id"])["status"] == "approved"


def test_settings_save(client):
    client.post("/settings", data={"csrf": client.csrf, "section": "money", "enabled": "on", "voice": "en-US-Chirp3-HD-Kore",
                                   "rate": "9", "brand": "MY BRAND", "slot_longA": "10:15", "max_long": "3", "max_short": "2"})
    ch = config.load_settings()["channels"]["money"]
    assert ch["voice"] == {"name": "en-US-Chirp3-HD-Kore", "rate": 1.3}
    assert ch["slots"]["longA"] == "10:15" and ch["theme"]["brand"] == "MY BRAND" and not ch["auto_publish"]
    client.post("/settings", data={"csrf": client.csrf, "section": "global", "emergency_stop": "on"})
    assert config.load_settings()["emergency_stop"]
    client.post("/settings", data={"csrf": client.csrf, "section": "global"})
    assert not config.load_settings()["emergency_stop"]


def test_upload_package_form(client):
    data = (SAMPLES / "sample-long-scam.json").read_bytes()
    r = client.post("/upload-package", data={"csrf": client.csrf, "file": (io.BytesIO(data), "p.json")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "اتضاف" in r.get_data(as_text=True)
    bad = json.dumps({"id": "x"}).encode()
    r = client.post("/upload-package", data={"csrf": client.csrf, "file": (io.BytesIO(bad), "b.json")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "مشكلة" in r.get_data(as_text=True)


def test_sources_save(client):
    client.post("/sources", data={"csrf": client.csrf, "channel": "money", "src_ssa_press": "on",
                                  "blocked": "politics, celebrity", "priority": "cola, medicare"})
    ch = config.load_settings()["channels"]["money"]
    on = {s["id"]: s["enabled"] for s in ch["sources"]}
    assert on["ssa_press"] and not on["irs_news"]
    assert ch["blocked_words"] == ["politics", "celebrity"]
