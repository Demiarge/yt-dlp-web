"""Public download links ("leases"). A file is only deleted once no lease keeps it."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse

from . import config
from .db import downloads_db

YOUTUBE_HOSTS = {"youtube.com", "youtube-nocookie.com"}
YOUTUBE_PATH_IDS = {"shorts", "live", "embed", "v", "e"}  # /shorts/<id> etc.
TRACKING_PARAMS = {"si", "feature", "fbclid", "gclid", "igshid", "pp", "ab_channel"}


def is_web_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:  # e.g. unclosed IPv6 bracket
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def source_key(url: str) -> str:
    """YouTube video ID, otherwise the normalized URL (query kept, many sites put the ID there)."""
    parsed = urlparse(url)
    host, path = (parsed.hostname or "").lower(), parsed.path
    candidates = []
    if host.removeprefix("www.").removeprefix("m.").removeprefix("music.") in YOUTUBE_HOSTS:
        candidates = [parse_qs(parsed.query).get("v", [""])[0]]
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in YOUTUBE_PATH_IDS:
            candidates.append(parts[1])
    elif host.removeprefix("www.") == "youtu.be":
        candidates = [path.strip("/").split("/", 1)[0]]
    for video_id in candidates:
        if video_id and config.YOUTUBE_ID_RE.fullmatch(video_id):
            return f"youtube:{video_id}"
    params = sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                    if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_"))
    query = f"?{urlencode(params)}" if params else ""
    with suppress(ValueError):
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    fragment = f"#{parsed.fragment}" if parsed.fragment.startswith(("/", "!")) else ""  # hash-routed sites
    return "url:" + hashlib.sha256(f"{parsed.scheme.lower()}://{host}{path}{query}{fragment}".encode()).hexdigest()[:32]


def minutes(value: str | None, default: int | None = None) -> int | None:
    """UI duration (minutes) -> seconds. None = forever."""
    if not value or value in ("lifetime", "permanent"):
        return None
    try:
        return max(1, min(int(value), config.MAX_DURATION_MINUTES)) * 60
    except (TypeError, ValueError):
        return default


def public_url_for(token: str, path: Path) -> str:
    return f"/public/{token}/{quote(path.name, safe='')}"  # quoted: titles can contain # and %


def public_token(public_url: str) -> str:
    parts = [part for part in urlparse(public_url).path.split("/") if part]
    return parts[1] if len(parts) >= 2 and parts[0] == "public" else ""


def insert_lease(con: sqlite3.Connection, path: Path, *, job_id: str, user_ip: str, source_url: str, source_key: str,
                 access_duration: str, file_retention: str, new_file: bool = False, reusable: bool = True) -> str:
    """reusable=False: incomplete download (e.g. failed subs), the next identical request downloads again."""
    now, original, path = int(time.time()), str(path), path.resolve()
    access, keep = minutes(access_duration, 3600), minutes(file_retention)
    token = secrets.token_urlsafe(18)
    public_url = public_url_for(token, path)
    if new_file:
        # old rows for this name belong to a file deleted by hand; their expiry mustn't delete this one
        con.execute("UPDATE downloads SET reusable=0 WHERE job_id IN "
                    "(SELECT job_id FROM downloads WHERE file_path IN (?,?) AND job_id IS NOT NULL)", (str(path), original))
        con.execute("DELETE FROM downloads WHERE file_path IN (?,?)", (str(path), original))
    con.execute("INSERT INTO downloads (token,user_ip,source_url,source_key,start_time,access_until,file_delete_at,"
                "file_path,public_url,access_enabled,file_retention,job_id,reusable) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (token, user_ip, source_url, source_key, now, access and now + access, keep and now + keep,
                 str(path), public_url, file_retention, job_id, int(reusable)))
    return public_url


def register_public_file(path: Path, **lease) -> str:
    with downloads_db.tx() as con:
        return insert_lease(con, path, new_file=True, **lease)


def lease_info(row) -> dict:
    now, access_until, delete_at = int(time.time()), row["access_until"], row["file_delete_at"]
    expired = (not row["access_enabled"] or (access_until is not None and now >= int(access_until))
               or (delete_at is not None and now >= int(delete_at)))
    return {"access_until": access_until, "file_delete_at": delete_at, "expired": bool(expired)}


def public_row_info(row) -> dict | None:
    path = Path(row["file_path"])
    return {"url": public_url_for(row["token"], path), **lease_info(row)} if path.is_file() else None


def find_lease(token: str):
    with downloads_db.tx() as con:
        return con.execute("SELECT * FROM downloads WHERE token=?", (token,)).fetchone()


def public_file_metadata(public_url: str) -> dict:
    token = public_token(public_url)
    if not token:
        return {}
    row = find_lease(token)
    return lease_info(row) if row else {"expired": True}


def decorate_complete_payload(payload: dict) -> dict:
    """Attach expiry info to every file of a "complete" event."""
    files = payload.get("files")
    if not isinstance(files, list):
        return payload
    return {**payload, "files": [{**f, **public_file_metadata(str(f.get("url") or ""))}
                                 for f in files if isinstance(f, dict)]}


def restore_job_result(result: dict | None, job_id: str) -> dict:
    """Refresh a job's file list from its leases, or rebuild it if the server died before saving it."""
    result = dict(result) if isinstance(result, dict) else {}
    files = result.get("files") if isinstance(result.get("files"), list) else []
    restored, changed = [], False
    with downloads_db.tx() as con:
        # each file's own lease, never another job's lease of the same file
        for item in files:
            if not isinstance(item, dict):
                continue
            token = public_token(str(item.get("url") or ""))
            row = con.execute("SELECT * FROM downloads WHERE token=?", (token,)).fetchone() if token else None
            info = (public_row_info(row) if row else None) or {"expired": True}
            if any(item.get(k) != v for k, v in info.items()):
                item, changed = {**item, **info}, True
            restored.append(item)

        if not restored:
            for row in con.execute("SELECT * FROM downloads WHERE job_id=? ORDER BY id", (job_id,)).fetchall():
                if info := public_row_info(row):
                    restored.append({"filename": Path(row["file_path"]).name, **info, "restored": True})
                    changed = True

    result["files"] = restored
    if changed:
        result["restored"] = True
    return result


def existing_download(job_id: str, source: str, variant: str, *, require_media: bool, user_ip: str,
                      access_duration: str, file_retention: str) -> list[tuple[Path, str]]:
    """Reuse every file of the newest complete earlier download of exactly this variant."""
    lease = {"job_id": job_id, "user_ip": user_ip, "access_duration": access_duration, "file_retention": file_retention}
    with downloads_db.tx() as con:
        groups: dict[str, list[Path]] = {}
        for row in con.execute("SELECT * FROM downloads WHERE source_key=? AND source_url=? AND reusable=1 "
                               "AND job_id IS NOT NULL ORDER BY id DESC", (source, variant)).fetchall():
            paths = groups.setdefault(row["job_id"], [])
            if Path(row["file_path"]) not in paths:
                paths.append(Path(row["file_path"]))
        for paths in groups.values():  # newest first
            paths.reverse()  # registration order: media first, then subtitles
            if not all(p.is_file() for p in paths):
                continue
            if require_media and not any(p.suffix.lower() in config.MEDIA_EXTS for p in paths):
                continue
            return [(p, insert_lease(con, p, source_url=variant, source_key=source, **lease)) for p in paths]
    return []


def cleanup_expired() -> None:
    """Drop rows whose file is gone, delete files past retention, disable expired links."""
    now, root = int(time.time()), config.DOWNLOADS.resolve()
    with downloads_db.tx() as con:
        for row in con.execute("SELECT id, job_id, file_path, access_until, file_delete_at, access_enabled "
                               "FROM downloads").fetchall():
            path = Path(row["file_path"])
            try:
                alive = path.resolve().is_relative_to(root) and path.is_file()
            except (OSError, ValueError):
                alive = False
            if not alive or (row["file_delete_at"] is not None and now >= row["file_delete_at"]):
                # Keep the file while another lease still wants it.
                if alive and not con.execute(
                        "SELECT 1 FROM downloads WHERE file_path=? AND id<>? AND (file_delete_at IS NULL OR file_delete_at > ?) LIMIT 1",
                        (str(path), row["id"], now)).fetchone():
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        continue  # Windows: file in use (being streamed), retry next round
                if row["job_id"]:
                    con.execute("UPDATE downloads SET reusable=0 WHERE job_id=?", (row["job_id"],))
                con.execute("DELETE FROM downloads WHERE id=?", (row["id"],))
            elif row["access_until"] is not None and now >= row["access_until"] and row["access_enabled"]:
                con.execute("UPDATE downloads SET access_enabled=0 WHERE id=?", (row["id"],))
