import sys
import time
import types

import pytest

from machine import config, db, ytanalyze


def fake_yt_dlp(blocked_ids=()):
    now = int(time.time())

    class YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if url.endswith("/videos"):
                ents = [{"id": f"v{i:02d}", "title": t, "url": f"https://www.youtube.com/watch?v=v{i:02d}",
                         "view_count": v, "duration": 600 + i * 30, "timestamp": now - (i + 1) * 3 * 86400,
                         "thumbnails": [{"url": f"https://i.ytimg.com/vi/v{i:02d}/hq.jpg", "width": 480, "height": 360}]}
                        for i, (t, v) in enumerate([
                            ("Social Security Payment Dates November 2026 (Official)", 900000),
                            ("SSI Payment Increase 2027: How Much More You Get", 450000),
                            ("why my check was late", 20000), ("Medicare Part B 2027 Premium", 60000),
                            ("random vlog day", 8000), ("IRS Tax Brackets 2027 Explained", 75000)])]
                return {"channel": "Test Money", "channel_id": "UC" + "x" * 22, "channel_follower_count": 12345,
                        "entries": ents[: self.opts.get("playlistend", 99)]}
            if url.endswith("/shorts"):
                return {"channel": "Test Money", "entries": [
                    {"id": "s1", "title": "SSI raise in 30 seconds #shorts", "url": "https://www.youtube.com/shorts/s1",
                     "view_count": 120000}]}
            if url.endswith("/streams"):
                raise Exception("This channel does not have a streams tab")
            vid = url.split("v=")[-1]
            if vid in blocked_ids:
                return None
            return {"id": vid, "title": None, "upload_date": "20261020", "timestamp": now - 10 * 86400,
                    "like_count": 1000, "comment_count": 50, "tags": ["social security", "ssi"],
                    "description": "Intro\n0:00 Start\n1:10 Dates\n3:20 Raise\nhttps://ssa.gov", "categories": ["News"],
                    "view_count": None, "duration": None}

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YDL
    return mod


def test_channel_base():
    assert ytanalyze.channel_base("@Foo") == "https://www.youtube.com/@Foo"
    assert ytanalyze.channel_base("https://www.youtube.com/@Foo/videos?x=1") == "https://www.youtube.com/@Foo"
    assert ytanalyze.channel_base("youtube.com/channel/UC" + "a" * 22).endswith("/channel/UC" + "a" * 22)
    with pytest.raises(ValueError):
        ytanalyze.channel_base("https://example.com/x")


def test_full_job_end_to_end(clean_db, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_yt_dlp(blocked_ids={"v05"}))
    jid = ytanalyze.new_job("https://www.youtube.com/@TestMoney", ["videos", "shorts", "streams"], "full", 100)
    job = ytanalyze.next_queued()
    assert job["id"] == jid
    rep = ytanalyze.run_job(job)
    assert ytanalyze.get_job(jid)["status"] == "done"
    vids = rep["videos"]
    assert len(vids) == 7 and {v["type"] for v in vids} == {"videos", "shorts"}
    top = max(vids, key=lambda r: r["views"] or 0)
    assert top["title"].startswith("Social Security Payment Dates") and top["likes"] == 1000
    assert top["tags"] == ["social security", "ssi"] and top["chapters"] == 3 and top["date_exact"]
    assert top["performance"].startswith("🔥")
    b = rep["summary"]["by_type"]["videos"]
    assert b["count"] == 6 and b["viral_count"] >= 1 and b["top10"][0]["views"] == 900000
    assert any(w["word"] in ("payment", "social", "security", "2026", "2027") for w in b["top_words"])
    assert (ytanalyze.REPORTS() / f"{jid}.xlsx").exists()
    assert any("streams" in w or "لايف" in w for w in rep["warnings"])


def test_fast_job_and_pages(clean_db, monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_yt_dlp())
    jid = ytanalyze.new_job("@TestMoney", ["videos"], "fast", 3)
    ytanalyze.run_job(ytanalyze.get_job(jid))
    rep = ytanalyze.load_report(jid)
    assert len(rep["videos"]) == 3 and not rep["videos"][0]["date_exact"]
    from machine.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    config.update_settings(lambda s: s.update({"password_hash": s.get("password_hash") or "x"}))
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["ok"] = True
            s["csrf"] = "t"
        assert "تحليل قناة يوتيوب" in c.get("/yt").get_data(as_text=True)
        html = c.get(f"/yt/{jid}").get_data(as_text=True)
        assert "Test Money" in html and "أعلى 10 مشاهدات" in html
        for fmt in ("xlsx", "csv", "json"):
            assert c.get(f"/yt/{jid}/download/{fmt}").status_code == 200
        r = c.post("/yt", data={"csrf": "t", "url": "not a channel", "tab_videos": "on", "mode": "fast"})
        assert r.status_code == 302
