from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# Settings you're meant to edit

SESSION_DAYS = 7  # logins don't survive a restart

MAX_PENDING_HASHES = 8  # parallel password checks (~64 MiB each), more get "busy"

# Per IP: this many wrong passwords within the window -> blocked for BLOCK_MINUTES.
MAX_FAILED_PASSWORDS = 5
FAILURE_WINDOW_MINUTES = 15
BLOCK_MINUTES = 5

# Same, over all IPs combined. 0 = off. Downside: an attacker can lock out remote users too.
MAX_FAILED_PASSWORDS_TOTAL = 30

# Localhost never gets locked out. Set False behind an ssh -L tunnel, where everyone looks local.
NEVER_BLOCK_LOCAL = True

# Trust X-Forwarded-For from 127.0.0.1 (nginx/Caddy/cloudflared). Set False for raw TCP tunnels
# (ssh -L, ngrok tcp, frp), the client can forge it there. uvicorn CLI: use --no-proxy-headers.
TRUST_PROXY_HEADERS = True

# Host names to answer to, e.g. ["localhost", "yt.example.com"]. Empty = any. Blocks DNS rebinding.
# Behind a proxy keep the port in Host (nginx: `proxy_set_header Host $http_host;`) or login fails.
ALLOWED_HOSTS: list[str] = []

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("YTDL_WEB_DATA") or ROOT)  # YTDL_WEB_DATA moves downloads, cookies and DBs elsewhere
INDEX = ROOT / "index.html"
DOWNLOADS, COOKIES = DATA / "downloads", DATA / "cookies"
DB_PATH, JOB_DB_PATH, AUTH_PATH = DATA / "downloads.sqlite3", DATA / "jobs.sqlite3", DATA / "auth.json"

YTDLP = os.environ.get("YTDLP_BIN") or shutil.which("yt-dlp") or "yt-dlp"
FFMPEG_DIR = os.environ.get("FFMPEG_DIR", "")  # folder with ffmpeg(.exe); empty = PATH

SESSION_COOKIE = "ytdlp_session"
COOKIE_RUN_PREFIX = ".run-"  # per-run cookie copies; leading "." hides them from the cookie list
DONE_FILE = ".done.txt"  # yt-dlp's --print-to-file target, lists final file paths

JOB_SESSION_TTL = 60 * 60
JOB_LOG_LIMIT = 300
PROGRESS_SAVE_INTERVAL = 1.0
MAX_DURATION_MINUTES = 10 * 365 * 24 * 60  # keeps timestamps inside SQLite's int64

MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v", ".flv",
              ".mp3", ".m4a", ".aac", ".opus", ".wav", ".flac", ".alac", ".ogg"}
SUBTITLE_EXTS = {".srt", ".vtt", ".ttml", ".ass", ".lrc", ".srv1", ".srv2", ".srv3", ".json3"}
AUTO_CONTAINERS = {"", "auto", "original"}
REMUX_CONTAINERS = {"mp4", "mkv", "webm", "mov", "avi", "flv"}  # what --remux-video accepts
AUDIO_FORMATS = {"aac", "alac", "flac", "m4a", "mp3", "opus", "vorbis", "wav"}  # what --audio-format accepts

DURATION_RE = re.compile(r"lifetime|permanent|\d{1,9}")  # minutes, or forever
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")  # token_urlsafe(18); the page generates its own
QUALITY_RE = re.compile(r"\b(\d{3,4}p)(?:\d{2,3})?\b")  # "1080p60" -> 1080p
LINE_END_RE = re.compile(r"\r\n|\r|\n")
