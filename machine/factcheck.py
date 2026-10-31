"""Literal fact check: every 'literal' fact must appear word-for-word in its official source page,
and every number shown or spoken must be backed by a fact. No AI involved here on purpose.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

from . import config
from .package import all_text

MONTHS = {"january": "jan", "february": "feb", "march": "mar", "april": "apr", "may": "may", "june": "jun",
          "july": "jul", "august": "aug", "september": "sep", "october": "oct", "november": "nov", "december": "dec"}
NUM_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")
ORD_RE = re.compile(r"\b(\d+)(st|nd|rd|th)\b")


def normalize(s: str) -> str:
    s = s.lower()
    s = s.replace("–", "-").replace("—", "-").replace("−", "-").replace(" ", " ")
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)
    s = ORD_RE.sub(r"\1", s)
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def variants(value: str) -> set[str]:
    base = normalize(value)
    out = {base}
    for full, short in MONTHS.items():
        if re.search(rf"\b{full}\b", base):
            out.add(re.sub(rf"\b{full}\b", short, base))
            out.add(re.sub(rf"\b{full}\b", short + ".", base))
        if re.search(rf"\b{short}\b", base):
            out.add(re.sub(rf"\b{short}\b", full, base))
    out |= {v.replace(" - ", " to ") for v in list(out)}
    out |= {v.replace(" - ", "-") for v in list(out)}
    return out


def _offline_path(url: str) -> Path:
    return config.DATA_DIR / "offline_sources" / (hashlib.sha1(url.encode()).hexdigest() + ".txt")


def fetch_text(url: str) -> str:
    if config.OFFLINE:
        p = _offline_path(url)
        if not p.exists():
            raise FileNotFoundError("offline source missing")
        return p.read_text(encoding="utf-8")
    from .google.http import request
    r = request("GET", url, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    ctype = r.headers.get("Content-Type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(r.content)
            f.flush()
            res = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True, timeout=120)
            return res.stdout
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.content, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return soup.get_text(" ")


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in NUM_RE.findall(normalize(text)):
        n = m.replace("$", "").replace("%", "").replace(",", "")
        out.add(n.rstrip("."))
    return out


def strip_allowed(text: str, allowed: list[str] | None) -> str:
    """Remove allowed phrases (e.g. the program name "Trump Accounts") before looking for blocked words."""
    low = text.lower()
    for ph in sorted(allowed or [], key=len, reverse=True):
        ph = ph.strip().lower()
        if ph:
            low = re.sub(rf"(?<![a-z]){re.escape(ph)}(?![a-z])", " ", low)
    return low


def find_blocked(text: str, words: list[str], allowed: list[str] | None = None) -> list[str]:
    low = " " + strip_allowed(text, allowed) + " "
    return [w for w in words if w.strip() and re.search(rf"(?<![a-z]){re.escape(w.strip().lower())}(?![a-z])", low)]


def blocked_hits(pkg: dict, words: list[str], allowed: list[str] | None = None) -> list[str]:
    txt = normalize(all_text(pkg) + " " + " ".join(s.get("vo", "") for s in pkg.get("scenes", [])))
    return find_blocked(txt, words, allowed)


def run(pkg: dict, blocked_words: list[str], allowed_phrases: list[str] | None = None) -> dict:
    cache: dict[str, str | None] = {}
    errors: dict[str, str] = {}

    def src(url: str) -> str | None:
        if url not in cache:
            try:
                cache[url] = normalize(fetch_text(url))
            except Exception as e:
                cache[url] = None
                errors[url] = str(e)[:200]
        return cache[url]

    facts_out = []
    covered: set[str] = set()
    for f in pkg.get("facts", []):
        kind = f.get("kind", "literal")
        covered |= numbers_in(f["value"])
        item = {"value": f["value"], "source": f["source"], "kind": kind, "note": f.get("note", "")}
        if kind == "derived":
            item["status"] = "derived"
        else:
            text = src(f["source"])
            if text is None:
                item["status"] = "unreachable"
                item["error"] = errors.get(f["source"], "")
            else:
                item["status"] = "found" if any(v in text for v in variants(f["value"])) else "missing"
        facts_out.append(item)

    spoken = " ".join(s.get("vo", "") for s in pkg.get("scenes", []))
    shown = all_text(pkg)
    uncovered = []
    for n in sorted(numbers_in(spoken + "\n" + shown)):
        try:
            val = float(n)
        except ValueError:
            continue
        if val <= 10 and float(int(val)) == val and n not in covered:
            continue   # small counters ("SIGN 1", "4 steps") are allowed
        if n not in covered:
            uncovered.append(n)

    blocked = blocked_hits(pkg, blocked_words, allowed_phrases)
    missing = [x for x in facts_out if x["status"] == "missing"]
    unreachable = [x for x in facts_out if x["status"] == "unreachable"]
    derived = [x for x in facts_out if x["status"] == "derived"]
    found = [x for x in facts_out if x["status"] == "found"]
    ok = not missing and not uncovered and not blocked and not unreachable
    return {
        "ok": ok,
        # auto-publish needs at least one fact actually confirmed in an official source
        "auto_ok": ok and not derived and len(found) >= 1,
        "facts": facts_out,
        "uncovered_numbers": uncovered,
        "blocked_words": blocked,
        "summary": summary_ar(facts_out, uncovered, blocked),
    }


def summary_ar(facts, uncovered, blocked) -> str:
    found = sum(1 for f in facts if f["status"] == "found")
    lit = sum(1 for f in facts if f["kind"] != "derived")
    der = sum(1 for f in facts if f["status"] == "derived")
    parts = [f"الأرقام الحرفية: {found} من {lit} موجودة في المصدر" if lit else "مفيش أرقام متسجلة للفحص (محتاج مراجعتك)"]
    if der:
        parts.append(f"{der} معلومة محسوبة (محتاجة نظرة منك)")
    if uncovered:
        parts.append("أرقام بدون مصدر: " + "، ".join(uncovered[:8]))
    if blocked:
        parts.append("كلمات ممنوعة: " + "، ".join(blocked))
    return " · ".join(parts)
