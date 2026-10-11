"""One shared SQLite connection per DB. Safe: every query runs on the event loop and no
transaction spans an await."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

DOWNLOADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    user_ip TEXT,
    source_url TEXT NOT NULL,      -- the download variant key, not the plain URL
    source_key TEXT NOT NULL,
    start_time INTEGER NOT NULL,
    access_until INTEGER,
    file_delete_at INTEGER,
    file_path TEXT NOT NULL,
    public_url TEXT NOT NULL,
    access_enabled INTEGER NOT NULL DEFAULT 1,
    file_retention TEXT NOT NULL DEFAULT 'permanent',
    job_id TEXT,                   -- groups the files of one download (video + its .srt)
    reusable INTEGER NOT NULL DEFAULT 1  -- 0: part of the group is gone, don't hand it out again
);
CREATE INDEX IF NOT EXISTS idx_downloads_source_key ON downloads(source_key);
CREATE INDEX IF NOT EXISTS idx_downloads_token ON downloads(token);
CREATE INDEX IF NOT EXISTS idx_downloads_job_id ON downloads(job_id);
"""

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    source_url TEXT NOT NULL,
    media_title TEXT,
    user_ip TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    progress TEXT,
    current_message TEXT,
    events_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT,
    error TEXT,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_expires_at ON jobs(expires_at);
"""


class Database:
    def __init__(self, path: Path, schema: str):
        self.path, self.schema = path, schema
        self._con: sqlite3.Connection | None = None

    def connection(self) -> sqlite3.Connection:
        if self._con is None:
            con = sqlite3.connect(self.path)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")  # crash-safe with WAL, no fsync per commit
            con.executescript(self.schema)
            self._con = con
        return self._con

    @contextmanager
    def tx(self):
        con = self.connection()
        with con:
            yield con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


downloads_db = Database(config.DB_PATH, DOWNLOADS_SCHEMA)
jobs_db = Database(config.JOB_DB_PATH, JOBS_SCHEMA)


def close_all() -> None:
    downloads_db.close()
    jobs_db.close()


def loads(text: str | None, default):
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default
