"""Radar: collects new items from official sources every 15 minutes (no AI), tiers them,
and publishes a candidates file to Drive for Claude's next run.

Tiers:
  1  an "awaiting" package's official number appeared  -> filled & rendered automatically
  2  important official news in the niche             -> picked up at Claude's next check-in run
  3  normal news                                       -> tomorrow's plan
  4  blocked topic                                     -> ignored
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

from . import config, db, events
from .google.http import request

NS_ATOM = "{http://www.w3.org/2005/Atom}"


def _get(url: str) -> bytes:
    url = url.replace("{today}", date.today().isoformat()).replace(
        "{days_ago_14}", (date.today() - timedelta(days=14)).isoformat()).replace(
        "{days_ago_7}", (date.today() - timedelta(days=7)).isoformat())
    if config.OFFLINE:
        p = config.DATA_DIR / "offline_feeds" / (hashlib.sha1(url.encode()).hexdigest() + ".dat")
        if not p.exists():
            raise FileNotFoundError("offline feed missing")
        return p.read_bytes()
    r = request("GET", url, timeout=30, retries=1)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.content


def parse_rss(raw: bytes) -> list[dict]:
    from lxml import etree
    root = etree.fromstring(raw, parser=etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    out = []
    for it in root.iter("item"):
        out.append({"title": (it.findtext("title") or "").strip(), "url": (it.findtext("link") or "").strip(),
                    "published": (it.findtext("pubDate") or "").strip()})
    for it in root.iter(NS_ATOM + "entry"):
        link = it.find(NS_ATOM + "link")
        out.append({"title": (it.findtext(NS_ATOM + "title") or "").strip(),
                    "url": link.get("href", "") if link is not None else "",
                    "published": (it.findtext(NS_ATOM + "updated") or it.findtext(NS_ATOM + "published") or "").strip()})
    return [x for x in out if x["title"]]


def parse_html_links(raw: bytes, base_url: str, contains: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, "lxml")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if contains and contains not in href:
            continue
        title = " ".join(a.get_text(" ").split())
        if len(title) < 18 or href in seen:
            continue
        seen.add(href)
        out.append({"title": title[:300], "url": href, "published": ""})
    return out[:60]


def _dig(obj, path: str):
    if not path:
        return obj
    for part in path.split("."):
        obj = obj.get(part, []) if isinstance(obj, dict) else []
    return obj


def parse_json_api(raw: bytes, src: dict) -> list[dict]:
    data = json.loads(raw)
    items = _dig(data, src.get("items_path", ""))
    if not isinstance(items, list):
        return []
    f = src.get("fields", {})
    out = []
    for it in items[:60]:
        if not isinstance(it, dict):
            continue
        title = str(it.get(f.get("title", "title"), "") or "").strip()
        if not title:
            continue
        first_line = title.splitlines()[0][:300]
        out.append({"title": first_line, "url": str(it.get(f.get("url", "url"), "") or src.get("default_url", "")),
                    "published": str(it.get(f.get("date", "date"), "") or ""),
                    "extra": str(it.get(f.get("extra", ""), "") or "")[:300] if f.get("extra") else "",
                    "id": str(it.get(f.get("id", "id"), "") or "")})
    return out


def parse_swpc_kp(raw: bytes, src: dict) -> list[dict]:
    """NOAA planetary K-index forecast: emit an item when a predicted Kp >= 5 (geomagnetic storm, aurora chance)."""
    rows = json.loads(raw)
    out = []
    if not rows or not isinstance(rows, list):
        return out
    header, body = rows[0], rows[1:]
    try:
        i_t, i_kp, i_obs = header.index("time_tag"), header.index("kp"), header.index("observed")
    except ValueError:
        return out
    by_day: dict[str, float] = {}
    for r in body:
        if r[i_obs] != "predicted":
            continue
        try:
            kp = float(r[i_kp])
        except (TypeError, ValueError):
            continue
        day = str(r[i_t])[:10]
        by_day[day] = max(by_day.get(day, 0), kp)
    for day, kp in sorted(by_day.items()):
        if kp >= 5:
            g = "G1" if kp < 6 else "G2" if kp < 7 else "G3" if kp < 8 else "G4" if kp < 9 else "G5"
            out.append({"title": f"NOAA forecast: Kp {kp:g} ({g} geomagnetic storm) on {day} — aurora possible",
                        "url": src.get("default_url", ""), "published": day, "id": f"kp-{day}-{kp:g}"})
    return out


def fetch_source(src: dict) -> list[dict]:
    raw = _get(src["url"])
    t = src["type"]
    if t == "rss":
        return parse_rss(raw)
    if t == "html_links":
        return parse_html_links(raw, src["url"], src.get("link_contains", ""))
    if t == "json_api":
        return parse_json_api(raw, src)
    if t == "swpc_kp":
        return parse_swpc_kp(raw, src)
    raise ValueError(f"unknown source type {t}")


def classify(title: str, ch: dict) -> int:
    low = title.lower()
    if any(re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", low) for w in ch.get("blocked_words", [])):
        return 4
    if any(w.lower() in low for w in ch.get("priority_keywords", [])):
        return 2
    return 3


def _trend_terms(cid: str) -> set[str]:
    rows = db.conn().execute("SELECT title FROM radar_items WHERE channel=? AND source_id='google_trends' AND first_seen>?",
                             (cid, time.time() - 2 * 86400)).fetchall()
    terms = set()
    for r in rows:
        for w in re.findall(r"[a-z0-9]{4,}", r["title"].lower()):
            terms.add(w)
    return terms


def run_channel(cid: str, ch: dict) -> list[dict]:
    """Fetch all enabled sources; return list of NEW items (tiers 1-3)."""
    new_items = []
    for src in ch.get("sources", []):
        if not src.get("enabled", True):
            continue
        now = time.time()
        try:
            items = fetch_source(src)
            db.conn().execute(
                "INSERT INTO source_status(channel,source_id,last_run,last_ok,last_error,last_count) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(channel,source_id) DO UPDATE SET last_run=excluded.last_run, last_ok=excluded.last_ok,"
                " last_error='', last_count=excluded.last_count", (cid, src["id"], now, now, "", len(items)))
        except Exception as e:
            db.conn().execute(
                "INSERT INTO source_status(channel,source_id,last_run,last_error) VALUES(?,?,?,?)"
                " ON CONFLICT(channel,source_id) DO UPDATE SET last_run=excluded.last_run, last_error=excluded.last_error",
                (cid, src["id"], now, str(e)[:300]))
            continue
        role = src.get("role", "news")
        for it in items:
            key = hashlib.sha1(f"{cid}|{src['id']}|{it.get('id') or it['url'] or it['title']}".encode()).hexdigest()
            tier = 0 if role == "trends" else classify(it["title"], ch)
            try:
                db.conn().execute(
                    "INSERT INTO radar_items(key,channel,source_id,title,url,published,extra,tier,first_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, cid, src["id"], it["title"], it.get("url", ""), it.get("published", ""), it.get("extra", ""), tier, now))
            except Exception:
                continue   # already seen
            if role != "trends" and tier in (2, 3):
                new_items.append({**it, "tier": tier, "source": src["name"], "key": key})
    # trend match flags for new items
    terms = _trend_terms(cid)
    for it in new_items:
        words = set(re.findall(r"[a-z0-9]{4,}", it["title"].lower()))
        if words & terms:
            it["trend_match"] = True
            db.conn().execute("UPDATE radar_items SET trend_match=1 WHERE key=?", (it["key"],))
    return new_items


def candidates_snapshot(cid: str, days: int = 7) -> dict:
    rows = db.conn().execute(
        "SELECT source_id,title,url,published,extra,tier,trend_match,first_seen FROM radar_items"
        " WHERE channel=? AND first_seen>? AND tier IN (0,2,3) ORDER BY first_seen DESC LIMIT 300",
        (cid, time.time() - days * 86400)).fetchall()
    items, trends = [], []
    for r in rows:
        d = dict(r)
        d["first_seen"] = datetime.fromtimestamp(d["first_seen"], timezone.utc).isoformat()
        (trends if d["tier"] == 0 else items).append(d)
    return {"channel": cid, "generated": datetime.now(timezone.utc).isoformat(), "items": items, "google_trends_us": trends[:40]}


def check_awaiting() -> None:
    """Tier 1: fill 'awaiting' packages when the official number shows up."""
    from .factcheck import fetch_text
    for p in db.pkg_list(status="awaiting"):
        aw = p["data"].get("awaiting") or {}
        last = db.kv_get(f"await_check:{p['id']}", 0)
        if time.time() - last < 60 * int(aw.get("check_every_min", 5)):
            continue
        db.kv_set(f"await_check:{p['id']}", time.time())
        try:
            text = fetch_text(aw["url"])
        except Exception:
            continue
        m = re.search(aw["regex"], text, flags=re.I | re.S)
        if not m:
            continue
        value = m.group(1) if m.groups() else m.group(0)
        value = " ".join(value.split())
        raw = json.dumps(p["data"], ensure_ascii=False).replace(aw.get("placeholder", "{{VALUE}}"), value)
        pkg = json.loads(raw)
        pkg.pop("awaiting", None)
        pkg["priority"] = "urgent"
        pkg["facts"] = pkg.get("facts", []) + [{"value": value, "source": aw["url"], "kind": "literal"}]
        db.pkg_update(p["id"], data=pkg, status="new", priority="urgent")
        events.info(f"🚨 الرقم الرسمي نزل ({value}) · الفيديو الجاهز اتملى وداخل التجهيز فوراً", p["channel"], notify=True)
