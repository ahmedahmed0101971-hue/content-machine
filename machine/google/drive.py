"""Google Drive bridge between Claude (writes packages) and the server (reads them, writes back data).

Folder layout (created by the server, so it can write into it):
  ContentMachine/
    _system/                       server -> Claude: settings_snapshot.json, candidates_<ch>.json, analytics_<ch>.json, notes_<ch>.json
    <channel>/packages/            Claude -> server: one JSON file per video
    <channel>/strategy.json        Claude -> server (optional)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .. import config
from .http import GoogleError, check, request
from .oauth import auth_headers

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_NAME = "ContentMachine"
TOKEN = "drive"


class Drive:
    def _h(self) -> dict:
        return auth_headers(TOKEN)

    def whoami(self) -> str:
        r = request("GET", f"{API}/about", params={"fields": "user(emailAddress,displayName)"}, headers=self._h())
        u = check(r, "قراءة حساب درايف").get("user", {})
        return u.get("emailAddress") or u.get("displayName", "")

    def _find(self, name: str, parent: str | None, folder: bool) -> str | None:
        q = [f"name = '{name}'", "trashed = false"]
        q.append(f"mimeType {'=' if folder else '!='} '{FOLDER_MIME}'")
        if parent:
            q.append(f"'{parent}' in parents")
        r = request("GET", f"{API}/files", params={"q": " and ".join(q), "fields": "files(id,name)", "pageSize": 10,
                                                   "spaces": "drive"}, headers=self._h())
        files = check(r, "البحث في درايف").get("files", [])
        return files[0]["id"] if files else None

    def ensure_folder(self, name: str, parent: str | None) -> str:
        fid = self._find(name, parent, True)
        if fid:
            return fid
        meta = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            meta["parents"] = [parent]
        r = request("POST", f"{API}/files", json=meta, params={"fields": "id"}, headers=self._h())
        return check(r, "إنشاء فولدر في درايف")["id"]

    def ensure_structure(self, channel_ids: list[str]) -> dict:
        root = self.ensure_folder(ROOT_NAME, None)
        folders = {"root": root, "_system": self.ensure_folder("_system", root)}
        for cid in channel_ids:
            chf = self.ensure_folder(cid, root)
            folders[cid] = chf
            folders[f"{cid}/packages"] = self.ensure_folder("packages", chf)
        return folders

    def list_files(self, folder_id: str) -> list[dict]:
        out, token = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 200,
                      "fields": "nextPageToken, files(id,name,mimeType,modifiedTime)", "orderBy": "createdTime"}
            if token:
                params["pageToken"] = token
            r = request("GET", f"{API}/files", params=params, headers=self._h())
            data = check(r, "قراءة الفولدر")
            out += [f for f in data.get("files", []) if f["mimeType"] != FOLDER_MIME]
            token = data.get("nextPageToken")
            if not token:
                return out

    def read_text(self, f: dict) -> str:
        if f.get("mimeType", "").startswith("application/vnd.google-apps."):
            r = request("GET", f"{API}/files/{f['id']}/export", params={"mimeType": "text/plain"}, headers=self._h())
        else:
            r = request("GET", f"{API}/files/{f['id']}", params={"alt": "media"}, headers=self._h())
        if r.status_code >= 400:
            check(r, "تنزيل ملف من درايف")
        r.encoding = "utf-8"
        return r.text

    def write_json(self, folder_id: str, name: str, obj) -> str:
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        existing = self._find(name, folder_id, False)
        if existing:
            r = request("PATCH", f"{UPLOAD}/files/{existing}", params={"uploadType": "media"}, data=body,
                        headers={**self._h(), "Content-Type": "application/json"})
            check(r, "تحديث ملف في درايف")
            return existing
        boundary = "cmboundary7x"
        meta = json.dumps({"name": name, "parents": [folder_id], "mimeType": "application/json"})
        multipart = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                     f"--{boundary}\r\nContent-Type: application/json\r\n\r\n").encode() + body + f"\r\n--{boundary}--".encode()
        r = request("POST", f"{UPLOAD}/files", params={"uploadType": "multipart", "fields": "id"}, data=multipart,
                    headers={**self._h(), "Content-Type": f"multipart/related; boundary={boundary}"})
        return check(r, "رفع ملف على درايف")["id"]


class LocalDrive:
    """Same interface backed by a local folder (MACHINE_OFFLINE=1: tests and demos)."""

    def __init__(self, base: Path | None = None):
        self.base = Path(base or config.LOCAL_DRIVE_DIR)

    def whoami(self) -> str:
        return "offline@local"

    def ensure_folder(self, name: str, parent: str | None) -> str:
        p = (Path(parent) if parent else self.base) / name
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def ensure_structure(self, channel_ids: list[str]) -> dict:
        root = self.ensure_folder("ContentMachine", None)
        folders = {"root": root, "_system": self.ensure_folder("_system", root)}
        for cid in channel_ids:
            chf = self.ensure_folder(cid, root)
            folders[cid] = chf
            folders[f"{cid}/packages"] = self.ensure_folder("packages", chf)
        return folders

    def list_files(self, folder_id: str) -> list[dict]:
        p = Path(folder_id)
        if not p.exists():
            return []
        return [{"id": str(f), "name": f.name, "mimeType": "application/json",
                 "modifiedTime": str(os.path.getmtime(f))}
                for f in sorted(p.iterdir(), key=lambda x: x.stat().st_mtime) if f.is_file()]

    def read_text(self, f: dict) -> str:
        return Path(f["id"]).read_text(encoding="utf-8")

    def write_json(self, folder_id: str, name: str, obj) -> str:
        p = Path(folder_id) / name
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(p)


def get_drive():
    if config.OFFLINE:
        return LocalDrive()
    if not config.load_token(TOKEN):
        raise GoogleError("جوجل درايف مش مربوط")
    return Drive()
