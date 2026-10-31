"""Paths and settings.

Settings live in DATA_DIR/settings.json. Secrets (OAuth tokens) live in DATA_DIR/tokens/ with 0600 permissions.
Everything the dashboard can change goes through load_settings()/save_settings().
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = Path(os.environ.get("MACHINE_DATA", ROOT_DIR / "data")).resolve()
FONTS_DIR = ROOT_DIR / "assets" / "fonts"
DEFAULTS_DIR = APP_DIR / "defaults"

SETTINGS_FILE = DATA_DIR / "settings.json"
TOKENS_DIR = DATA_DIR / "tokens"
WORK_DIR = DATA_DIR / "work"          # renders in progress / finished videos
DB_FILE = DATA_DIR / "machine.db"
LOCAL_DRIVE_DIR = DATA_DIR / "local_drive"   # used when MACHINE_OFFLINE=1 (tests / demo)

OFFLINE = os.environ.get("MACHINE_OFFLINE") == "1"

_lock = threading.RLock()

DEFAULT_SLOTS = {"short1": "07:00", "longA": "09:00", "longB": "12:00", "longC": "15:00", "short2": "18:00"}


def ensure_dirs() -> None:
    for d in (DATA_DIR, TOKENS_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(TOKENS_DIR, 0o700)
    except OSError:
        pass


def _default_channels() -> dict:
    with open(DEFAULTS_DIR / "channels.json", encoding="utf-8") as f:
        return json.load(f)


def default_settings() -> dict:
    return {
        "version": 1,
        "password_hash": "",
        "secret_key": secrets.token_hex(32),
        "google": {"client_id": "", "client_secret": "", "tts_api_key": ""},
        "telegram": {"bot_token": "", "chat_id": ""},
        "emergency_stop": False,
        "youtube_audited": False,   # until Google approves the API audit, uploads stay private
        "drive": {"connected": False, "email": "", "root_id": "", "folders": {}},
        "channels": _default_channels(),
    }


def _merge_defaults(cur: dict, dflt: dict) -> dict:
    """Add keys that exist in defaults but are missing in the saved settings (after an update)."""
    for k, v in dflt.items():
        if k not in cur:
            cur[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(cur[k], dict) and k != "channels":
            _merge_defaults(cur[k], v)
    if "channels" in dflt:
        for cid, ch in dflt["channels"].items():
            if cid in cur["channels"]:
                _merge_defaults(cur["channels"][cid], ch)
    return cur


def load_settings() -> dict:
    with _lock:
        ensure_dirs()
        if not SETTINGS_FILE.exists():
            s = default_settings()
            save_settings(s)
            return s
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s = json.load(f)
        return _merge_defaults(s, default_settings())


def save_settings(s: dict) -> None:
    with _lock:
        ensure_dirs()
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass


def update_settings(fn) -> dict:
    """Read-modify-write under the lock: update_settings(lambda s: s.update(...))."""
    with _lock:
        s = load_settings()
        fn(s)
        save_settings(s)
        return s


def channel(s: dict, cid: str) -> dict:
    return s["channels"][cid]


def token_path(name: str) -> Path:
    return TOKENS_DIR / f"{name}.json"


def load_token(name: str) -> dict | None:
    p = token_path(name)
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_token(name: str, data: dict) -> None:
    ensure_dirs()
    p = token_path(name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def delete_token(name: str) -> None:
    p = token_path(name)
    if p.exists():
        p.unlink()
