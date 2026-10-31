"""Google OAuth for a headless server (copy-paste loopback flow with PKCE).

The user opens the consent link in their own browser, approves, gets redirected to
http://127.0.0.1:8765/?code=... (a page that "doesn't load" — that's expected),
copies the address and pastes it into the dashboard. We exchange the code here.
Requires an OAuth client of type "Desktop app".
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

from .. import config, db
from .http import GoogleError, check, request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT = "http://127.0.0.1:8765/"

SCOPES = {
    "youtube": [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ],
    "drive": [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "openid", "email",
    ],
}


def _client() -> tuple[str, str]:
    g = config.load_settings()["google"]
    if not g.get("client_id") or not g.get("client_secret"):
        raise GoogleError("لازم تحط Client ID و Client Secret الأول في صفحة الإعداد")
    return g["client_id"], g["client_secret"]


def start(kind: str, token_name: str) -> str:
    """Return the consent URL. kind = 'youtube' | 'drive'."""
    cid, _ = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    db.kv_set(f"oauth:{state}", {"verifier": verifier, "token_name": token_name, "kind": kind, "ts": time.time()})
    params = {
        "client_id": cid, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": " ".join(SCOPES[kind]), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "include_granted_scopes": "false",
    }
    return AUTH_URL + "?" + urlencode(params)


def parse_pasted(pasted: str) -> tuple[str, str]:
    pasted = pasted.strip()
    q = parse_qs(urlparse(pasted).query)
    if "error" in q:
        raise GoogleError("جوجل رفض الإذن: " + q["error"][0])
    if "code" not in q or "state" not in q:
        raise GoogleError("الرابط اللي لزقته مش كامل. انسخ العنوان كله من شريط المتصفح بعد الموافقة.")
    return q["code"][0], q["state"][0]


def finish(pasted: str) -> tuple[str, dict]:
    code, state = parse_pasted(pasted)
    pending = db.kv_get(f"oauth:{state}")
    if not pending:
        raise GoogleError("الرابط ده قديم أو مش من الجلسة دي. ابدأ الربط من الأول.")
    cid, secret = _client()
    r = request("POST", TOKEN_URL, data={
        "client_id": cid, "client_secret": secret, "code": code, "code_verifier": pending["verifier"],
        "grant_type": "authorization_code", "redirect_uri": REDIRECT,
    })
    tok = check(r, "تبادل الإذن")
    if "refresh_token" not in tok:
        raise GoogleError("جوجل مرجّعش refresh token. احذف إذن التطبيق من حسابك وجرّب تاني.")
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    tok["kind"] = pending["kind"]
    config.save_token(pending["token_name"], tok)
    db.kv_set(f"oauth:{state}", None)
    return pending["token_name"], tok


def access_token(token_name: str) -> str:
    tok = config.load_token(token_name)
    if not tok:
        raise GoogleError("الحساب ده مش مربوط")
    if tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    cid, secret = _client()
    r = request("POST", TOKEN_URL, data={
        "client_id": cid, "client_secret": secret, "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    })
    if r.status_code == 400 and "invalid_grant" in r.text:
        raise GoogleError("إذن جوجل انتهى أو اتلغى. اربط الحساب تاني من صفحة الإعداد "
                          "(ولو بيتكرر كل أسبوع: حوّل التطبيق لوضع Production في Google Cloud).", 400, r.text)
    new = check(r, "تجديد الإذن")
    tok["access_token"] = new["access_token"]
    tok["expires_at"] = time.time() + int(new.get("expires_in", 3600)) - 60
    config.save_token(token_name, tok)
    return tok["access_token"]


def auth_headers(token_name: str) -> dict:
    return {"Authorization": f"Bearer {access_token(token_name)}"}
