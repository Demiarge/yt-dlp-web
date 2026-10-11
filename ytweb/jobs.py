"""Download jobs: their DB rows, live state and the event stream to the tabs following them."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import suppress

from fastapi import WebSocket

from . import config
from .db import jobs_db, loads
from .leases import decorate_complete_payload, restore_job_result

# Batched to the DB at most once per PROGRESS_SAVE_INTERVAL; the socket still gets every line.
BATCHED_EVENTS = {"output", "log", "command", "warning"}


class JobRegistry:
    def __init__(self) -> None:
        self.sockets: dict[str, set[WebSocket]] = {}  # every tab that started or resumed the job
        self.running: set[str] = set()
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.cancel_requested: set[str] = set()
        self._last_progress_save: dict[str, float] = {}
        self._last_event_save: dict[str, float] = {}
        self._pending_events: dict[str, list[dict]] = {}

    def attach(self, job_id: str, websocket: WebSocket) -> None:
        self.sockets.setdefault(job_id, set()).add(websocket)

    def detach(self, job_id: str, websocket: WebSocket) -> None:
        sockets = self.sockets.get(job_id)
        if sockets is not None:
            sockets.discard(websocket)
            if not sockets:
                self.sockets.pop(job_id, None)

    def create(self, action: str, source_url: str, user_ip: str, media_title: str = "", requested_id: str = "") -> str:
        """The page picks the id itself, so it can resume even if job_started never arrives."""
        now = int(time.time())
        job_id = requested_id if config.JOB_ID_RE.fullmatch(requested_id) else secrets.token_urlsafe(18)
        with jobs_db.tx() as con:
            if con.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
                job_id = secrets.token_urlsafe(18)
            con.execute("INSERT INTO jobs (job_id,action,source_url,media_title,user_ip,status,created_at,updated_at,expires_at) "
                        "VALUES (?,?,?,?,?,'queued',?,?,?)",
                        (job_id, action, source_url, media_title, user_ip, now, now, now + config.JOB_SESSION_TTL))
        return job_id

    def update(self, job_id: str, *, events: list[dict] | None = None, **fields) -> None:
        now = int(time.time())
        fields = {k: v for k, v in fields.items() if v is not None}
        if "result" in fields:
            fields["result_json"] = json.dumps(fields.pop("result"))
        fields.update(updated_at=now, expires_at=now + config.JOB_SESSION_TTL)
        with jobs_db.tx() as con:
            if events:
                row = con.execute("SELECT events_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if not row:
                    return
                events = loads(row["events_json"], []) + events
                fields["events_json"] = json.dumps(events[-config.JOB_LOG_LIMIT:], ensure_ascii=False)
            con.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE job_id=?", [*fields.values(), job_id])

    def flush(self, job_id: str, extra: list[dict] = (), **fields) -> None:
        events = [*self._pending_events.pop(job_id, []), *extra]
        self._last_event_save[job_id] = time.monotonic()
        if events or fields:
            self.update(job_id, events=events, **fields)

    def media(self, job_id: str) -> dict:
        with jobs_db.tx() as con:
            row = con.execute("SELECT source_url, media_title FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return {"source_url": row["source_url"], "media_title": row["media_title"] or ""} if row else {}

    def snapshot(self, job_id: str) -> dict | None:
        """State for a page that reloads mid-job."""
        if self._pending_events.get(job_id):
            self.flush(job_id)
        with jobs_db.tx() as con:
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        result, status, error = loads(row["result_json"], None), row["status"], row["error"] or ""
        # queued/running but not ours = killed by a restart, maybe right after publishing files
        orphaned = status in {"queued", "running"} and job_id not in self.running
        if status == "completed" or orphaned:
            with suppress(Exception):
                saved = result
                result = restore_job_result(result, job_id)
                if result.get("files"):
                    if status != "completed" or result != saved:  # every write also pushes expiry back
                        self.update(job_id, status="completed", current_message="Download complete.", result=result)
                    status = "completed"
        if orphaned and status != "completed":
            status, error = "failed", "The server restarted while this download was running."
            self.update(job_id, status=status, current_message=error, error=error)
        return {"status": status, "source_url": row["source_url"], "media_title": row["media_title"] or "",
                "progress": row["progress"] or "", "events": loads(row["events_json"], []),
                "result": result, "error": error}

    async def emit(self, job_id: str, payload: dict) -> None:
        payload = {**payload, "job_id": job_id}
        kind, message = payload.get("type", ""), str(payload.get("message") or "")
        if kind == "progress":
            now = time.monotonic()
            if now - self._last_progress_save.get(job_id, 0.0) >= config.PROGRESS_SAVE_INTERVAL:
                self._last_progress_save[job_id] = now
                self.update(job_id, progress=message)
        elif kind == "complete":
            # Label files with the job's own source/title, not whatever the page fetched since.
            payload = decorate_complete_payload({**self.media(job_id), **payload})
            self.flush(job_id, [payload], status="completed", current_message="Download complete.", result=payload)
        elif kind == "cancelled":
            self.flush(job_id, [payload], status="cancelled", current_message=message)
        elif kind == "error":
            self.flush(job_id, [payload], status="failed", current_message=message, error=message)
        elif kind in BATCHED_EVENTS:
            # A playlist prints thousands of lines; one DB write each would stall the server.
            self._pending_events.setdefault(job_id, []).append(payload)
            if kind == "warning" or time.monotonic() - self._last_event_save.get(job_id, 0.0) >= config.PROGRESS_SAVE_INTERVAL:
                self.flush(job_id, status="running", current_message=message)
        else:
            self.flush(job_id, [payload])

        for ws in list(self.sockets.get(job_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                self.detach(job_id, ws)

    def finished(self, job_id: str) -> None:
        with suppress(Exception):
            self.flush(job_id)  # normally empty, the final event wrote everything
        self.running.discard(job_id)
        self.cancel_requested.discard(job_id)
        self.processes.pop(job_id, None)
        self._last_progress_save.pop(job_id, None)
        self._last_event_save.pop(job_id, None)

    def cleanup(self) -> None:
        now = int(time.time())
        with jobs_db.tx() as con:
            con.execute("DELETE FROM jobs WHERE expires_at <= ? AND status IN ('completed','failed','cancelled')", (now,))
            # rows left by a dead server; live jobs are kept however quiet
            stale = con.execute("SELECT job_id FROM jobs WHERE expires_at <= ? AND status IN ('queued','running') "
                                "AND updated_at <= ?", (now, now - config.JOB_SESSION_TTL * 2)).fetchall()
            con.executemany("DELETE FROM jobs WHERE job_id=?",
                            [(row["job_id"],) for row in stale if row["job_id"] not in self.running])


registry = JobRegistry()
