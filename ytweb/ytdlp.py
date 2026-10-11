"""Running yt-dlp and turning its -J output into what the page shows."""
from __future__ import annotations

import asyncio
import codecs
import json
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import WebSocket

from . import config
from .jobs import registry as jobs
from .leases import YOUTUBE_HOSTS
from .procs import kill_process_tree, new_session_kwargs, watch_process_group


class LineSplitter:
    """Lone \\r = progress redraw, \\n or \\r\\n = a normal line."""

    def __init__(self) -> None:
        self.buffer = ""
        self.scanned = 0  # buffer[:scanned] has no line end; -J prints one huge line, don't rescan it
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk: bytes) -> list[tuple[str, str]]:
        self.buffer += self.decoder.decode(chunk)
        out, start = [], 0
        while match := config.LINE_END_RE.search(self.buffer, max(start, self.scanned)):
            if match.group() == "\r" and match.end() == len(self.buffer):
                break  # could be the first half of \r\n
            text, start = self.buffer[start:match.start()], match.end()
            out.append(("progress" if match.group() == "\r" else "line", text))
        self.buffer = self.buffer[start:]
        self.scanned = len(self.buffer) - self.buffer.endswith("\r")
        return out

    def close(self) -> str:
        rest = (self.buffer + self.decoder.decode(b"", final=True)).removesuffix("\r")
        self.buffer, self.scanned = "", 0
        return rest


def base_command(cookie_path: Path | None) -> list[str]:
    # --encoding: Windows pipes otherwise use the ANSI code page and mangle CJK titles.
    # --ignore-config: a stray yt-dlp.conf mustn't change anything.
    return [config.YTDLP, "--ignore-config", "--encoding", "utf-8",
            *(["--ffmpeg-location", config.FFMPEG_DIR] if config.FFMPEG_DIR else []),
            *(["--cookies", str(cookie_path)] if cookie_path else [])]


async def run_yt_dlp(args: list[str], *, job_id: str | None = None, websocket: WebSocket | None = None,
                     cookie_path: Path | None = None, stream_output: bool = True) -> tuple[int, list[str]]:
    """Output goes to the job, or straight to the socket for fetches. -> (exit code, output lines)"""
    async def emit(payload):
        if job_id:
            await jobs.emit(job_id, payload)
        elif websocket is not None:
            with suppress(Exception):
                await websocket.send_json(payload)

    if job_id in jobs.cancel_requested:
        return -1, []
    await emit({"type": "command", "message": "Running yt-dlp..."})
    process = await asyncio.create_subprocess_exec(
        *base_command(cookie_path), *args, cwd=str(config.ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **new_session_kwargs())
    watch_process_group(process.pid, True)
    if job_id:
        jobs.processes[job_id] = process
        if job_id in jobs.cancel_requested:
            await kill_process_tree(process)

    lines: list[str] = []
    splitter = LineSplitter()
    try:
        while chunk := await process.stdout.read(65536):
            for kind, text in splitter.feed(chunk):
                if kind == "progress":
                    if stream_output and text:
                        await emit({"type": "progress", "message": text})
                    continue
                if text:
                    lines.append(text)
                if stream_output:
                    await emit({"type": "output", "message": text, "line": text})
        if rest := splitter.close():
            lines.append(rest)
            if stream_output:
                await emit({"type": "output", "message": rest, "line": rest})
        return await process.wait(), lines
    finally:
        if job_id:
            jobs.processes.pop(job_id, None)
        if process.returncode is None:  # cancelled / failed mid-read
            await kill_process_tree(process)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 10)
        watch_process_group(process.pid, False)


async def check_ytdlp() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(config.YTDLP, "--version",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError:
        raise RuntimeError(f"yt-dlp not found ({config.YTDLP}). Set YTDLP_BIN to its full path.") from None
    except NotImplementedError:  # Windows selector loop (uvicorn --reload / --workers) can't spawn processes
        raise RuntimeError("This event loop cannot start yt-dlp. On Windows, run `python server.py`, "
                           "or uvicorn without --reload and --workers.") from None
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp did not start: {out.decode(errors='replace').strip()}")
    return out.decode(errors="replace").strip()


async def update_ytdlp() -> None:
    """Only the standalone yt-dlp binary can update itself."""
    with suppress(OSError):
        proc = await asyncio.create_subprocess_exec(config.YTDLP, "-U", **new_session_kwargs())
        try:
            await asyncio.wait_for(proc.wait(), 120)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)


async def fetch_info(websocket: WebSocket, url: str, cookie_path: Path | None, what: str, tag: dict) -> dict | None:
    # --no-playlist: just the video of a &list= link. --flat-playlist: don't resolve every playlist entry.
    code, lines = await run_yt_dlp(["-J", "--no-warnings", "--skip-download", "--no-playlist", "--flat-playlist", "--", url],
                                   websocket=websocket, cookie_path=cookie_path, stream_output=False)
    error = f"yt-dlp failed while fetching {what}."
    if code == 0:
        try:  # stderr shares the pipe, pick the JSON line
            info = json.loads(next((line for line in reversed(lines) if line.lstrip().startswith("{")), ""))
        except json.JSONDecodeError:
            info, error = None, f"Could not parse {what}."
        if isinstance(info, dict):
            return info
    elif reason := next((line.strip() for line in reversed(lines) if line.startswith("ERROR:")), ""):
        error = f"{error} {reason}"
    await websocket.send_json({"type": "error", "message": error, **tag})
    return None


def is_playlist(info: dict) -> bool:
    return info.get("_type") in ("playlist", "multi_video") and not info.get("formats")


def in_youtube_list(url: str) -> bool:
    """YouTube video opened from a playlist (&list=)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    return host in YOUTUBE_HOSTS | {"youtu.be"} and bool(parse_qs(parsed.query).get("list", [""])[0])


def playlist_message(info: dict, url: str) -> dict:
    entries = info.get("entries")
    count = info.get("playlist_count") or (len(entries) if isinstance(entries, list) else None)
    return {"type": "playlist", "playlist": {"title": str(info.get("title") or "").strip(), "count": count, "url": url}}


def formats_message(info: dict, url: str) -> dict:
    formats = []
    for f in info.get("formats", []):
        vcodec, acodec, ext = f.get("vcodec"), f.get("acodec"), str(f.get("ext") or "")
        if vcodec is None and acodec not in (None, "none") and not (f.get("width") or f.get("height")):
            vcodec = "none"  # audio-only HLS rendition that only names its audio codec
        # unknown codec (None) counts as present, like yt-dlp does
        has_video, has_audio = vcodec != "none", acodec != "none"
        if ext.lower() == "mhtml" or not (has_video or has_audio):
            continue
        note = str(f.get("format_note") or "")
        # prefer yt-dlp's label (1080p even when height is 1078)
        quality = config.QUALITY_RE.search(note) or config.QUALITY_RE.search(str(f.get("resolution") or ""))
        formats.append({
            "id": str(f.get("format_id") or ""), "ext": ext,
            "resolution": str(f.get("resolution") or ("" if has_video else "audio only")),
            "width": f.get("width"), "height": f.get("height"), "quality_label": quality.group(1) if quality else "",
            "format_note": note, "language": str(f.get("language") or ""),
            "filesize": f.get("filesize") or f.get("filesize_approx"), "abr": f.get("abr"),
            "tbr": f.get("tbr"), "protocol": str(f.get("protocol") or ""), "vcodec": str(vcodec or ""),
            "acodec": str(acodec or ""), "kind": "video" if has_video else "audio", "combined": has_video and has_audio,
        })
    media = {"title": str(info.get("title") or info.get("fulltitle") or "").strip(),
             "url": str(info.get("webpage_url") or info.get("original_url") or url).strip()}
    return {"type": "formats", "formats": formats, "media": media}


def subtitles_message(info: dict, _url: str) -> dict:
    merged: dict[str, dict] = {}
    for key, auto in (("subtitles", False), ("automatic_captions", True)):
        for code, entries in (info.get(key) or {}).items():
            entries = [e for e in entries or [] if isinstance(e, dict)]
            item = merged.setdefault(code, {"code": code, "formats": set(), "manual": False, "auto": False,
                                            "translated": False, "chat": code == "live_chat"})
            item["auto" if auto else "manual"] = True
            item["formats"].update(str(e["ext"]) for e in entries if e.get("ext"))
            # YouTube machine translations (~100). SELECT ALL skips them, fetching all = 429.
            if auto and entries and all("tlang=" in str(e.get("url") or "") for e in entries):
                item["translated"] = True
    subtitles = [{**item, "formats": sorted(item["formats"]), "translated": item["translated"] and not item["manual"]}
                 for item in merged.values()]
    subtitles.sort(key=lambda x: (x["code"].lower() != "en", x["code"].lower()))
    return {"type": "subtitles", "subtitles": subtitles}


# action -> (kind, label for errors, message builder)
FETCHERS = {"fetch_formats": ("formats", "format information", formats_message),
            "fetch_subtitles": ("subtitles", "subtitle information", subtitles_message)}
