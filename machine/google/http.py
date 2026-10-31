"""HTTP helpers with retries and readable errors."""
from __future__ import annotations

import time

import requests

UA = "ContentMachine/1.0 (+self-hosted; contact via channel)"


class GoogleError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


def request(method: str, url: str, *, retries: int = 3, timeout: int = 60, **kw) -> requests.Response:
    headers = kw.pop("headers", {}) or {}
    headers.setdefault("User-Agent", UA)
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = GoogleError(f"مشكلة اتصال: {e}")
        else:
            if r.status_code in (429, 500, 502, 503, 504):
                last = GoogleError(f"الخدمة مشغولة ({r.status_code})", r.status_code, r.text[:500])
            else:
                return r
        if attempt < retries:
            time.sleep(min(30, 2 ** attempt * 2))
    raise last  # type: ignore[misc]


def check(r: requests.Response, what: str) -> dict:
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error_description") or body.get("error")
        except ValueError:
            msg = r.text[:300]
        raise GoogleError(f"{what}: {msg}", r.status_code, r.text[:1000])
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}
