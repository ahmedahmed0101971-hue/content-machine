"""Event log (shown in the dashboard, in Arabic) + optional Telegram notifications."""
from __future__ import annotations

import logging
import threading

from . import config, db

log = logging.getLogger("machine")


def _telegram(text: str) -> None:
    if config.OFFLINE:
        return
    s = config.load_settings()
    tg = s.get("telegram", {})
    if not tg.get("bot_token") or not tg.get("chat_id"):
        return
    import requests
    try:
        requests.post(f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                      json={"chat_id": tg["chat_id"], "text": text, "disable_web_page_preview": True}, timeout=15)
    except Exception as e:  # never let notifications break the machine
        log.warning("telegram failed: %s", e)


def info(msg: str, channel: str | None = None, notify: bool = False) -> None:
    db.event("info", msg, channel)
    log.info("[%s] %s", channel or "-", msg)
    if notify:
        threading.Thread(target=_telegram, args=(msg,), daemon=True).start()


def warn(msg: str, channel: str | None = None, notify: bool = True) -> None:
    db.event("warn", msg, channel)
    log.warning("[%s] %s", channel or "-", msg)
    if notify:
        threading.Thread(target=_telegram, args=("⚠️ " + msg,), daemon=True).start()


def error(msg: str, channel: str | None = None, notify: bool = True) -> None:
    db.event("error", msg, channel)
    log.error("[%s] %s", channel or "-", msg)
    if notify:
        threading.Thread(target=_telegram, args=("❌ " + msg,), daemon=True).start()
