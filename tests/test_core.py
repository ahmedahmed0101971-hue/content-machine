import time
import copy
import json

from conftest import SSA_COLA_URL, load_sample, put_feed, put_source

from machine import config, db, factcheck, package, pipeline, radar
from machine.google import oauth
from machine.google.tts import mock_wav
from machine.render import audio


def test_samples_are_valid():
    s = config.load_settings()
    for name in ("sample-long-ssa-november", "sample-short-cola", "sample-long-scam"):
        assert package.validate(load_sample(name), s) == [], name


def test_validation_catches_problems():
    s = config.load_settings()
    p = load_sample("sample-short-cola")
    p["id"] = "bad id!"
    p["slot"] = "midnight"
    p["scenes"][0]["type"] = "spinning_logo"
    p["youtube"]["title"] = "x" * 120
    errs = package.validate(p, s)
    assert len(errs) >= 4
    assert all(isinstance(e, str) for e in errs)


def test_publish_time_handles_daylight_saving():
    ch = config.load_settings()["channels"]["money"]
    p = {"slot": "longA", "date": "2026-11-02"}           # EST (UTC-5)
    assert package.publish_time(p, ch).isoformat() == "2026-11-02T14:00:00+00:00"
    p = {"slot": "longA", "date": "2026-10-15"}           # EDT (UTC-4)
    assert package.publish_time(p, ch).isoformat() == "2026-10-15T13:00:00+00:00"


def test_factcheck_passes_when_numbers_are_in_source():
    res = factcheck.run(load_sample("sample-short-cola"), [])
    assert res["ok"], res
    assert res["auto_ok"], res
    assert all(f["status"] == "found" for f in res["facts"])


def test_factcheck_catches_wrong_and_unsourced_numbers():
    p = load_sample("sample-short-cola")
    p["facts"][0]["value"] = "3.9"                         # not in the source
    p["scenes"][1]["vo"] += " Some say 4.1 percent next year."   # number with no fact
    res = factcheck.run(p, [])
    assert not res["ok"]
    assert any(f["status"] == "missing" for f in res["facts"])
    assert "4.1" in res["uncovered_numbers"]


def test_factcheck_derived_needs_human():
    res = factcheck.run(load_sample("sample-long-ssa-november"), [])
    assert not res["auto_ok"]
    assert any(f["status"] == "derived" for f in res["facts"])


def test_factcheck_without_facts_is_not_auto():
    res = factcheck.run(load_sample("sample-long-scam"), [])
    assert res["ok"] and not res["auto_ok"]


def test_blocked_words():
    p = load_sample("sample-short-cola")
    p["scenes"][0]["vo"] = "Claim your free money now with this stimulus check."
    hits = factcheck.blocked_hits(p, ["free money", "stimulus check", "cure"])
    assert set(hits) == {"free money", "stimulus check"}


def test_normalize_variants():
    assert "nov 18" in factcheck.variants("November 18th")
    assert factcheck.normalize("$1,234.50 – 21st") == "$1234.50 - 21"


def test_oauth_parse_pasted():
    code, state = oauth.parse_pasted("http://127.0.0.1:8765/?state=abc&code=4/xyz&scope=s")
    assert (code, state) == ("4/xyz", "abc")
    for bad in ("http://127.0.0.1:8765/", "hello", "http://127.0.0.1:8765/?error=access_denied"):
        try:
            oauth.parse_pasted(bad)
            assert False, bad
        except Exception:
            pass


def test_audio_helpers():
    a, sr = audio.wav_to_array(mock_wav("x" * 150))
    assert sr == 24000 and 9.5 < audio.duration(a) < 10.5
    padded = audio.np.concatenate([audio.np.zeros(24000, audio.np.float32), a, audio.np.zeros(24000, audio.np.float32)])
    assert abs(audio.duration(audio.trim_silence(padded)) - audio.duration(a)) < 0.3


def test_srt():
    out = pipeline.srt([(0.25, 2.5, "Hello"), (2.5, 65.0, "World")])
    assert "00:00:00,250 --> 00:00:02,500" in out and "00:00:02,500 --> 00:01:05,000" in out


RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Social Security announces COLA for 2027 recipients</title><link>https://ex.gov/a</link><pubDate>Wed, 14 Oct 2026</pubDate></item>
<item><title>Agency picnic photo gallery for employees</title><link>https://ex.gov/b</link></item>
<item><title>Election results of the employee union vote</title><link>https://ex.gov/c</link></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Medicare Part B premium set</title>
<link href="https://ex.gov/d"/><updated>2026-11-01</updated></entry></feed>"""
HTML = b"""<html><body><a href="/news/en/press/releases/2026-10-14.html">Social Security Announces 3.6 Percent Benefit Increase</a>
<a href="/about">About us</a><a href="/news/en/press/releases/2026-10-01.html">Short</a></body></html>"""
JSON_API = json.dumps([{"RecallID": 1, "Title": "Kids car seat recalled due to choking hazard", "RecallDate": "2026-10-20",
                        "URL": "https://cpsc.gov/r/1"}]).encode()
KP = json.dumps([["time_tag", "kp", "observed", "noaa_scale"],
                 ["2026-11-01 00:00:00", "3.33", "observed", None],
                 ["2026-11-02 03:00:00", "6.67", "predicted", "G2"]]).encode()
TRENDS = b"""<?xml version="1.0"?><rss><channel><item><title>social security cola</title></item></channel></rss>"""


def test_parsers():
    assert len(radar.parse_rss(RSS)) == 3
    assert radar.parse_rss(ATOM)[0]["url"] == "https://ex.gov/d"
    links = radar.parse_html_links(HTML, "https://www.ssa.gov/news/en/press/releases/", "/news/en/press/releases/20")
    assert len(links) == 1 and links[0]["url"].startswith("https://www.ssa.gov/news/en/press/releases/2026-10-14")
    items = radar.parse_json_api(JSON_API, {"items_path": "", "fields": {"id": "RecallID", "title": "Title", "date": "RecallDate", "url": "URL"}})
    assert items[0]["title"].startswith("Kids car seat")
    kp = radar.parse_swpc_kp(KP, {"default_url": "https://swpc"})
    assert len(kp) == 1 and "G2" in kp[0]["title"]


def test_radar_run_tiers_and_dedup(clean_db):
    ch = copy.deepcopy(config.load_settings()["channels"]["money"])
    ch["sources"] = [
        {"id": "google_trends", "name": "trends", "type": "rss", "role": "trends", "url": "https://t.example/rss", "enabled": True},
        {"id": "feed", "name": "feed", "type": "rss", "url": "https://f.example/rss", "enabled": True},
        {"id": "broken", "name": "broken", "type": "rss", "url": "https://broken.example/rss", "enabled": True},
    ]
    put_feed("https://t.example/rss", TRENDS)
    put_feed("https://f.example/rss", RSS)
    radar.run_channel("money", ch)        # first pass also stores the trends
    clean_db.execute("DELETE FROM radar_items WHERE source_id='feed'")
    new = radar.run_channel("money", ch)
    tiers = {i["title"][:20]: i["tier"] for i in new}
    assert tiers["Social Security anno"] == 2
    assert tiers["Agency picnic photo "] == 3
    assert all("Election" not in i["title"] for i in new)          # blocked -> tier 4, not returned
    assert any(i.get("trend_match") for i in new)
    assert radar.run_channel("money", ch) == []                     # nothing new the second time
    st = {r["source_id"]: dict(r) for r in clean_db.execute("SELECT * FROM source_status")}
    assert st["broken"]["last_error"] and st["feed"]["last_count"] == 3
    snap = radar.candidates_snapshot("money")
    assert snap["items"] and snap["google_trends_us"]


def test_awaiting_fills_value(clean_db):
    url = "https://www.irs.gov/newsroom/limits-2027"
    p = load_sample("sample-short-cola")
    p["id"] = "await-test-401k"
    p["scenes"][1]["vo"] = "The new 401k limit for 2027 is {{VALUE}} dollars."
    p["awaiting"] = {"url": url, "regex": r"limit .*? is \$([\d,]+)", "placeholder": "{{VALUE}}", "check_every_min": 0}
    db.pkg_insert(p, "awaiting")
    put_source(url, "Nothing yet")
    radar.check_awaiting()
    assert db.pkg_get("await-test-401k")["status"] == "awaiting"
    put_source(url, "The elective deferral limit for 2027 is $25,500 under the new rules.")
    db.kv_set("await_check:await-test-401k", 0)
    radar.check_awaiting()
    got = db.pkg_get("await-test-401k")
    assert got["status"] == "new" and got["priority"] == "urgent"
    assert "25,500" in got["data"]["scenes"][1]["vo"]
    assert got["data"]["facts"][-1]["value"] == "25,500"


def test_daily_limit(clean_db):
    config.update_settings(lambda s: s["channels"]["money"]["limits"].update({"max_short": 2}))
    try:
        for k in range(3):
            p = load_sample("sample-short-cola")
            p["id"] = f"limit-test-{k}"
            db.pkg_insert(p, "new")
        pipeline.check_new()
        sts = sorted(db.pkg_get(f"limit-test-{k}")["status"] for k in range(3))
        assert sts.count("rejected") == 1
    finally:
        config.update_settings(lambda s: s["channels"]["money"]["limits"].update({"max_short": 5}))


def test_recover(clean_db):
    from machine import worker
    p = load_sample("sample-short-cola")
    p["id"] = "recover-test"
    db.pkg_insert(p, "rendering")
    worker.recover()
    assert db.pkg_get("recover-test")["status"] == "approved"


def test_thumbnail_rules_and_no_truncation(tmp_path):
    from machine.render.thumbnail import _fields, render_thumbnail
    s = config.load_settings()
    p = load_sample("sample-long-ssa-november")
    p["thumbnail"] = {"tile_top": "MEDICARE", "tile_value": "$202.90", "words": ["NEW", "PREMIUM"]}
    assert package.validate(p, s) == []
    assert _fields(p)[1] == "$202.90"
    render_thumbnail(p, s["channels"]["money"]["theme"], str(tmp_path / "t.jpg"))
    p["thumbnail"] = {"tile_top": "NOVEMBER2026", "tile_value": "$1,234.56", "words": ["A", "B", "C", "D", "E"]}
    errs = package.validate(p, s)
    assert len([e for e in errs if "الصورة المصغرة" in e]) == 3


def test_youtube_meta_shorts_and_sources():
    p = load_sample("sample-short-cola")
    p["youtube"]["title"] = "COLA 2027 confirmed"
    m = pipeline.youtube_meta(p)
    assert m["title"].endswith("#Shorts")
    assert p["sources"][0]["url"] in m["description"]
    assert p["youtube"]["title"] == "COLA 2027 confirmed"      # original untouched


def test_upload_pauses_on_quota(clean_db, monkeypatch):
    config.update_settings(lambda s: s.update({"youtube_audited": True}) or
                           s["channels"]["money"]["youtube"].update({"connected": True}))
    try:
        p = load_sample("sample-short-cola")
        p["id"] = "quota-test"
        db.pkg_insert(p, "ready")
        db.pkg_update("quota-test", video_path="/nonexistent.mp4")

        def boom(*a, **k):
            raise Exception('403 {"reason": "uploadLimitExceeded"}')
        monkeypatch.setattr(pipeline.youtube, "upload", boom)
        assert pipeline.upload_next() is False
        got = db.pkg_get("quota-test")
        assert got["status"] == "ready" and not got.get("attempts")
        assert db.kv_get("upload_pause_until", 0) > time.time()
        assert pipeline.upload_next() is False                     # paused
    finally:
        db.kv_set("upload_pause_until", 0)
        config.update_settings(lambda s: s.update({"youtube_audited": False}) or
                               s["channels"]["money"]["youtube"].update({"connected": False}))


def test_moved_source_url_is_fixed():
    def fn(s):
        for src in s["channels"]["money"]["sources"]:
            if src["id"] == "bls_news":
                src["url"] = "https://www.bls.gov/feed/news_release.rss"
    config.update_settings(fn)
    src = [x for x in config.load_settings()["channels"]["money"]["sources"] if x["id"] == "bls_news"][0]
    assert src["url"] == "https://www.bls.gov/feed/bls_latest.rss"


def test_chapters_follow_youtube_rules():
    p = load_sample("sample-long-ssa-november")
    for k, sc in enumerate(p["scenes"]):
        sc["chapter"] = f"Chapter {k}"
    tl = [{"start": s, "end": e} for s, e in [(0, 8.4), (8.4, 40.2), (40.2, 47.0), (47.0, 70.1), (70.1, 95.5), (95.5, 104)]]
    chs = pipeline.chapters(p, tl)
    # intro (8.4 s) merges with the 31.8 s scene -> 00:00 takes its title; 6.8 s scene 2 merges into scene 3
    assert chs == [(0, "Chapter 1"), (41, "Chapter 3"), (71, "Chapter 4")]
    assert all(b[0] - a[0] >= 10 for a, b in zip(chs, chs[1:]))
    assert 104 - chs[-1][0] >= 10
    p["_chapters"] = chs
    d = pipeline.youtube_meta(p)["description"]
    first, _, rest = d.partition("\n\n")
    assert rest.startswith("Chapters:\n00:00 Chapter 1\n00:41 Chapter 3\n01:11 Chapter 4")
    assert pipeline.fmt_ts(3725) == "1:02:05"


def test_no_chapters_for_shorts_or_too_few():
    p = load_sample("sample-short-cola")
    assert pipeline.chapters(p, [{"start": 0, "end": 30}] * len(p["scenes"])) == []
    p = load_sample("sample-long-ssa-november")
    tl = [{"start": 0, "end": 5}, {"start": 5, "end": 12}] + [{"start": 12, "end": 14}] * (len(p["scenes"]) - 2)
    assert pipeline.chapters(p, tl) == []


def test_html_links_skip_menu_items():
    html = b"""<a href="/news/press-releases">View All Press Releases</a>
    <a href="/news/press-releases/statements-remarks">Remarks and Statements</a>
    <a href="/news/press-releases/x1">Secretary Statements &amp; Remarks</a>
    <a href="/news/press-releases/sb0301">Treasury Announces New Fraud Prevention Measures for Benefit Payments</a>"""
    got = radar.parse_html_links(html, "https://home.treasury.gov/news/press-releases", "/news/press-releases/")
    assert [g["title"] for g in got] == ["Treasury Announces New Fraud Prevention Measures for Benefit Payments"]
