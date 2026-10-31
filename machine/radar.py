"""Radar: collects new items from official sources every 15 minutes (no AI), tiers them,
and publishes a candidates file to Drive for Claude's next run.

Tiers:
  1  an "awaiting" package's official number appeared  -> filled & rendered automatically
  2  important official news in the niche             -> picked up at Claude's next check-in run
  3  normal news                                       -> tomorrow's plan
  4  blocked topic                                     -> ignored
  5  old / baseline (archive item, or undated item present at the first look) -> hidden
Only items published within MAX_AGE_DAYS (trends: 2 days) are ever stored as candidates.
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
MAX_AGE_DAYS = 14
TIER_BASELINE = 5


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


# ------------------------------------------------------------------ dates (so only FRESH news counts)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_D_ISO = re.compile(r"(?<!\d)(20\d{2})[-/_.](\d{1,2})[-/_.](\d{1,2})(?!\d)")
_D_COMPACT = re.compile(r"/(20\d{2})(\d{2})(\d{2})(?!\d)")
_D_US = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)")
_D_TEXT = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", re.I)
_D_TEXT_EU = re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+(20\d{2})\b", re.I)


def _mk(y, m, d) -> datetime | None:
    try:
        dt = datetime(int(y), int(m), int(d), 12, 0, tzinfo=timezone.utc)
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    return dt if datetime(2000, 1, 1, tzinfo=timezone.utc) <= dt <= now + timedelta(days=2) else None


def parse_date(s: str) -> datetime | None:
    """Best-effort date from RSS/Atom/HTML text or a URL. None if there is no trustworthy date."""
    if not s:
        return None
    s = str(s).strip()
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)                       # RFC 822 (RSS pubDate)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO (Atom, JSON APIs)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for rx, order in ((_D_ISO, "ymd"), (_D_COMPACT, "ymd"), (_D_US, "mdy")):
        m = rx.search(s)
        if m:
            g = m.groups()
            dt = _mk(*g) if order == "ymd" else _mk(g[2], g[0], g[1])
            if dt:
                return dt
    m = _D_TEXT.search(s)
    if m:
        return _mk(m.group(3), MONTHS[m.group(1)[:3].lower()], m.group(2))
    m = _D_TEXT_EU.search(s)
    if m:
        return _mk(m.group(3), MONTHS[m.group(2)[:3].lower()], m.group(1))
    return None


def item_date(it: dict) -> datetime | None:
    return parse_date(it.get("published", "")) or parse_date(it.get("url", ""))


def parse_rss(raw: bytes) -> list[dict]:
    from lxml import etree
    root = etree.fromstring(raw, parser=etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    out = []
    for it in root.iter("item"):
        out.append({"title": clean_title((it.findtext("title") or "").strip()), "url": (it.findtext("link") or "").strip(),
                    "published": (it.findtext("pubDate") or "").strip()})
    for it in root.iter(NS_ATOM + "entry"):
        link = it.find(NS_ATOM + "link")
        out.append({"title": (it.findtext(NS_ATOM + "title") or "").strip(),
                    "url": link.get("href", "") if link is not None else "",
                    "published": (it.findtext(NS_ATOM + "updated") or it.findtext(NS_ATOM + "published") or "").strip()})
    return [x for x in out if x["title"]]


_NAV_RE = re.compile(r"^(view|see|read|show|browse|more|all)\b|\b(press releases|remarks and statements|"
                     r"statements (and|&) remarks|news releases|archive|subscribe|sign up|rss)\b", re.I)


def _is_nav(title: str, href: str, base_url: str) -> bool:
    """Menu/listing links ("View All Press Releases", "Remarks and Statements"), not news items."""
    if href.rstrip("/") == base_url.rstrip("/"):
        return True
    if len(title.split()) < 5:
        return True
    return bool(_NAV_RE.search(title)) and len(title.split()) < 8


_LEAD_JUNK = re.compile(r"^(?:(?:read more|learn more|more)\s*(?:about|on)?\s*[:\-–]?\s*|"
                        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}\s*[|:\-–]?\s*|"
                        r"\d{1,2}/\d{1,2}/20\d{2}\s*[|:\-–]?\s*)+", re.I)
_TAIL_JUNK = re.compile(r"\s*(?:[|:\-–]\s*)?(?:read more|learn more|read)\s*[.…>»]*$", re.I)


def clean_title(t: str) -> str:
    """'October 30, 2026 Social Security …' / 'Read more about Plan Year 2027 …' -> the headline itself."""
    t = _TAIL_JUNK.sub("", _LEAD_JUNK.sub("", t)).strip()
    return t[:1].upper() + t[1:] if t else t


def parse_html_links(raw: bytes, base_url: str, contains: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, "lxml")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if contains and contains not in href:
            continue
        title = clean_title(" ".join(a.get_text(" ").split()))
        if len(title) < 18 or href in seen or _is_nav(title, href, base_url):
            continue
        seen.add(href)
        out.append({"title": title[:300], "url": href, "published": _html_date(a, href, contains)})
        if len(out) >= 600:
            break
    # archive pages can list years of releases in any order: newest dated items first, then undated
    out.sort(key=lambda x: x["published"] or "", reverse=True)
    return out[:80]


def _html_date(a, href: str, contains: str) -> str:
    """Date for a news link: from its URL, or from the smallest surrounding block that holds only this link."""
    dt = parse_date(href) or parse_date(a.get_text(" "))
    if not dt:
        node = a
        for _ in range(5):
            node = node.parent
            if node is None or node.name in ("body", "html", "[document]"):
                break
            links = [x for x in node.find_all("a", href=True) if not contains or contains in x["href"]]
            if len({x["href"] for x in links}) > 1:
                break                                        # block holds other news links -> date is ambiguous
            t = node.find("time")
            if t is not None:
                dt = parse_date(t.get("datetime", "")) or parse_date(t.get_text(" "))
            dt = dt or parse_date(node.get_text(" "))
            if dt:
                break
    return dt.isoformat() if dt else ""


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
    from .factcheck import find_blocked
    low = title.lower()
    if find_blocked(title, ch.get("blocked_words", []), ch.get("allowed_phrases", [])):
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
        max_age = float(src.get("max_age_days", 2 if role == "trends" else ch.get("max_age_days", MAX_AGE_DAYS)))
        oldest = datetime.now(timezone.utc) - timedelta(days=max_age)
        first_run = not db.conn().execute("SELECT 1 FROM radar_items WHERE channel=? AND source_id=? LIMIT 1",
                                          (cid, src["id"])).fetchone()
        for it in items:
            dt = item_date(it)
            if dt and dt < oldest:
                continue                     # old news (archive pages list years of releases): never a candidate
            key = hashlib.sha1(f"{cid}|{src['id']}|{it.get('id') or it['url'] or it['title']}".encode()).hexdigest()
            tier = 0 if role == "trends" else classify(it["title"], ch)
            if first_run and not dt and role != "trends":
                tier = TIER_BASELINE         # undated item already on the page the first time we looked: not news
            try:
                db.conn().execute(
                    "INSERT INTO radar_items(key,channel,source_id,title,url,published,extra,tier,first_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, cid, src["id"], it["title"], it.get("url", ""), dt.isoformat() if dt else "",
                     it.get("extra", ""), tier, now))
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
        " WHERE channel=? AND first_seen>? AND tier IN (0,2,3) ORDER BY first_seen DESC LIMIT 400",
        (cid, time.time() - days * 86400)).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items, trends = [], []
    for r in rows:
        d = dict(r)
        pub = parse_date(d["published"]) if d["published"] else None
        if pub and pub < cutoff:
            continue
        d["first_seen"] = datetime.fromtimestamp(d["first_seen"], timezone.utc).isoformat()
        d["published"] = pub.isoformat() if pub else ""
        d["age_hours"] = round((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 1) if pub else None
        (trends if d["tier"] == 0 else items).append(d)
    items.sort(key=lambda x: x["published"] or x["first_seen"], reverse=True)
    return {"channel": cid, "generated": datetime.now(timezone.utc).isoformat(),
            "note": f"Only items published in the last {days} days. age_hours = hours since the official publish time.",
            "items": items[:200], "google_trends_us": trends[:40]}


def hide_old_items(days: int = MAX_AGE_DAYS) -> int:
    """One-off cleanup (runs at worker start): hide items that turn out to be old by their date or URL."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    for r in db.conn().execute("SELECT key,published,url FROM radar_items WHERE tier IN (2,3)").fetchall():
        dt = parse_date(r["published"] or "") or parse_date(r["url"] or "")
        if dt and dt < cutoff:
            db.conn().execute("UPDATE radar_items SET tier=? WHERE key=?", (TIER_BASELINE, r["key"]))
            n += 1
    return n


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
