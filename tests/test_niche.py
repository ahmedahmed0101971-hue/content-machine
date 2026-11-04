import sys
import types
from datetime import datetime, timedelta, timezone

from machine import config, db, niche


def fake_search_module():
    class YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            assert "search_query=" in url
            ents = []
            for i in range(30):
                ch = f"UC{'new' if i % 3 == 0 else 'old'}{i % 6:02d}" + "x" * 17
                ents.append({"id": f"vid{i:08d}", "title": f"citizenship test video {i}", "view_count": 200000 if i % 3 == 0 else 50000,
                             "duration": 2400, "channel_id": ch, "channel": f"Chan {i % 6}", "url": f"https://www.youtube.com/watch?v=vid{i:08d}"})
            ents.append({"id": "UCchannelresult", "title": "a channel row"})       # non-video rows are ignored
            return {"entries": ents}
    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YDL
    return mod


def test_search_url_filters():
    assert "sp=CAMSAggF" in niche.search_url("us citizenship", "views", "year")
    assert "sp=" not in niche.search_url("x", "relevance", "any")
    assert "search_query=us+citizenship+test" in niche.search_url("us citizenship test", "relevance", "any")


def test_niche_job_scores_open_niche(clean_db, monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_search_module())
    now = datetime.now(timezone.utc)

    def fake_channels(ids):
        out = {}
        for cid in ids:
            new = "new" in cid
            out[cid] = {"title": cid[:8], "handle": "@" + cid[:8], "created": (now - timedelta(days=150 if new else 3000)).isoformat(),
                        "country": "US", "description": "", "avatar": "", "subs": 40000 if new else 2_000_000,
                        "total_views": 6_000_000 if new else 900_000_000, "videos": 60 if new else 1500, "uploads": "UU" + cid[2:]}
        return out
    monkeypatch.setattr(niche, "channels_info", fake_channels)
    monkeypatch.setattr(niche, "recent_uploads", lambda pl, n=30: [
        {"id": "a", "title": "t", "published": now.isoformat(), "views": 30000, "duration": 2400, "short": False}])
    jid = niche.new_job("us citizenship test 2026\nn-400 interview", "relevance", "year", 30, 12, 5)
    rep = niche.run_job(niche.get_job(jid))
    n = rep["niche"]
    assert n["channels_seen"] == 6 and n["new_channels"] == 2 and n["new_winners"] == 2
    assert n["share_views_new_channels_pct"] > 50 and n["giants"] == 4
    assert 0 <= n["score"] <= 100 and n["verdict"]
    top = rep["channels"][0]
    assert top["is_new"] and top["momentum"] >= max(c["momentum"] or 0 for c in rep["channels"][1:])
    assert rep["channels"][0].get("recent", {}).get("last30_uploads") == 1
    assert (niche.reports_dir() / f"{jid}.xlsx").exists()


def test_niche_pages(clean_db, monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_search_module())
    monkeypatch.setattr(niche, "channels_info", lambda ids: {})
    jid = niche.new_job("budget tips", "views", "month", 20, 6, 0)
    niche.run_job(niche.get_job(jid))
    from machine.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    config.update_settings(lambda s: s.update({"password_hash": s.get("password_hash") or "x"}))
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["ok"] = True
            s["csrf"] = "t"
        assert "باحث النيشات" in c.get("/niche").get_data(as_text=True)
        html = c.get(f"/niche/{jid}").get_data(as_text=True)
        assert "درجة الفرصة" in html and "ربط قناة" in html
        assert c.get(f"/niche/{jid}/download/xlsx").status_code == 200
        assert c.post("/niche", data={"csrf": "t", "queries": "  "}).status_code == 302
        assert "value=\"https://x\"" in c.get("/yt?url=https://x").get_data(as_text=True)
