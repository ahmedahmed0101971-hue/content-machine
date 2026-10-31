"""SQLite storage. One file, safe for the web process and the worker process together (WAL mode)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    format TEXT NOT NULL,
    slot TEXT,
    pkg_date TEXT,
    priority TEXT DEFAULT 'normal',
    drive_file_id TEXT,
    data TEXT NOT NULL,
    status TEXT NOT NULL,
    check_result TEXT,
    note TEXT DEFAULT '',
    publish_at TEXT,
    video_path TEXT,
    thumb_path TEXT,
    youtube_id TEXT,
    error TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pkg_status ON packages(status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    channel TEXT,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS radar_items (
    key TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    published TEXT,
    extra TEXT,
    tier INTEGER,
    trend_match INTEGER DEFAULT 0,
    first_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radar_channel ON radar_items(channel, first_seen);
CREATE TABLE IF NOT EXISTS source_status (
    channel TEXT NOT NULL,
    source_id TEXT NOT NULL,
    last_run REAL,
    last_ok REAL,
    last_error TEXT,
    last_count INTEGER,
    PRIMARY KEY (channel, source_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Package statuses and their Arabic labels (dashboard)
STATUS_AR = {
    "new": "جديد",
    "needs_review": "بانتظار مراجعتك",
    "approved": "معتمد · في الطابور",
    "rendering": "جاري التجهيز",
    "ready": "جاهز للرفع",
    "uploading": "جاري الرفع",
    "scheduled": "مجدول على يوتيوب",
    "published": "منشور",
    "private": "اترفع خاص · انشره من يوتيوب ستوديو",
    "rejected": "مرفوض",
    "failed": "فشل",
    "awaiting": "مستني الرقم الرسمي",
}


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.ensure_dirs()
        c = sqlite3.connect(config.DB_FILE, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        _local.conn = c
    return c


def reset_connection() -> None:
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


# ---------------- kv ----------------
def kv_get(key: str, default=None):
    r = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def kv_set(key: str, value) -> None:
    conn().execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))


# ---------------- packages ----------------
def pkg_insert(pkg: dict, status: str, drive_file_id: str | None = None, check: dict | None = None) -> bool:
    now = time.time()
    try:
        conn().execute(
            "INSERT INTO packages(id,channel,format,slot,pkg_date,priority,drive_file_id,data,status,check_result,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pkg["id"], pkg["channel"], pkg["format"], pkg.get("slot"), pkg.get("date"), pkg.get("priority", "normal"),
             drive_file_id, json.dumps(pkg, ensure_ascii=False), status,
             json.dumps(check, ensure_ascii=False) if check else None, now, now))
        return True
    except sqlite3.IntegrityError:
        return False


def pkg_get(pid: str) -> dict | None:
    r = conn().execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    d["data"] = json.loads(d["data"])
    d["check_result"] = json.loads(d["check_result"]) if d.get("check_result") else None
    return d


def pkg_update(pid: str, **fields) -> None:
    if "data" in fields and not isinstance(fields["data"], str):
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
    if "check_result" in fields and fields["check_result"] is not None and not isinstance(fields["check_result"], str):
        fields["check_result"] = json.dumps(fields["check_result"], ensure_ascii=False)
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn().execute(f"UPDATE packages SET {cols} WHERE id=?", (*fields.values(), pid))


def pkg_list(status: str | tuple | None = None, channel: str | None = None, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM packages WHERE 1=1", []
    if status:
        sts = (status,) if isinstance(status, str) else tuple(status)
        q += f" AND status IN ({','.join('?' * len(sts))})"
        args += list(sts)
    if channel:
        q += " AND channel=?"
        args.append(channel)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in conn().execute(q, args).fetchall()]


def pkg_claim_next(from_status: str, to_status: str) -> dict | None:
    """Atomically take the next package in a status (urgent first, then earliest publish time)."""
    with tx() as c:
        r = c.execute(
            "SELECT id FROM packages WHERE status=? ORDER BY CASE priority WHEN 'urgent' THEN 0 ELSE 1 END,"
            " COALESCE(publish_at,'9999'), created_at LIMIT 1", (from_status,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE packages SET status=?, updated_at=? WHERE id=? AND status=?",
                  (to_status, time.time(), r["id"], from_status))
    return pkg_get(r["id"])


def count_for_day(channel: str, pkg_date: str, fmt: str) -> int:
    r = conn().execute(
        "SELECT COUNT(*) n FROM packages WHERE channel=? AND pkg_date=? AND format=? AND status NOT IN ('rejected','failed')",
        (channel, pkg_date, fmt)).fetchone()
    return r["n"]


# ---------------- events ----------------
def event(level: str, message: str, channel: str | None = None) -> None:
    conn().execute("INSERT INTO events(ts,level,channel,message) VALUES(?,?,?,?)", (time.time(), level, channel, message))


def events(limit: int = 200, level: str | None = None) -> list[dict]:
    if level:
        rows = conn().execute("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit))
    else:
        rows = conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows.fetchall()]


def prune(days: int = 60) -> None:
    cutoff = time.time() - days * 86400
    conn().execute("DELETE FROM events WHERE ts<?", (cutoff,))
    conn().execute("DELETE FROM radar_items WHERE first_seen<?", (cutoff,))
