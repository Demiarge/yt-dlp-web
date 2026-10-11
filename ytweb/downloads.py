"""The download actions: video, audio, subtitles, playlist."""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
from contextlib import suppress
from pathlib import Path

from . import config
from .cookies import cookie_file
from .jobs import registry as jobs
from .leases import existing_download, register_public_file, source_key
from .ytdlp import run_yt_dlp

def normalize_subtitle_languages(value) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").split(",")
    return list(dict.fromkeys(code for code in (str(item).strip() for item in raw) if code))


def subtitle_args(languages: list[str], mode: str) -> list[str]:
    if not languages:
        return []
    # manual + auto captions; VTT for embedding because WebM only takes VTT
    embed = mode == "embed"
    return ["--sub-langs", ",".join(languages), "--write-subs", "--write-auto-subs",
            "--convert-subs", "vtt" if embed else "srt", *(["--embed-subs"] if embed else [])]


# --remux-video only copies streams, so steer auto picks to codecs the container can hold.
# Exact format IDs can still fail; the file then stays in its original container.
REMUX_SORT = {"mov": "vcodec:h264,acodec:aac", "avi": "vcodec:h264,acodec:aac", "webm": "ext:webm:webm"}


def container_args(container: str) -> list[str]:
    if container in config.AUTO_CONTAINERS:
        return []
    sort = REMUX_SORT.get(container)
    return ["--remux-video", container, *(["-S", f"lang,{sort}"] if sort else [])]


def remux_failed(lines: list[str]) -> bool:
    """The last step before yt-dlp's first ERROR was the remux."""
    first_error = next((i for i, line in enumerate(lines) if line.startswith("ERROR:")), None)
    if first_error is None:
        return False
    steps = [line for line in lines[:first_error] if not line.startswith("WARNING:")]
    return bool(steps) and steps[-1].startswith("[VideoRemuxer] Remuxing")


def job_files(job_dir: Path) -> list[Path]:
    return [p for p in job_dir.rglob("*")
            if p.is_file() and not p.name.startswith(".") and not p.name.endswith((".part", ".ytdl"))]


def printed_paths(job_dir: Path) -> list[Path]:
    """Final paths yt-dlp wrote via --print-to-file."""
    done = job_dir / config.DONE_FILE
    if not done.is_file():
        return []
    lines = done.read_text(encoding="utf-8").splitlines()
    return [p for p in (Path(line.strip()) for line in lines if line.strip()) if p.is_file()]


def newest_media(files: list[Path]) -> Path | None:
    # final file is written last; X.temp.mp4 is a failed ffmpeg step
    return max((p for p in files if p.suffix.lower() not in config.SUBTITLE_EXTS and not p.stem.endswith(".temp")),
               key=lambda p: p.stat().st_mtime_ns, default=None)


def srt_files(files: list[Path]) -> list[Path]:
    return sorted((p for p in files if p.suffix.lower() == ".srt"), key=lambda p: p.name.lower())


def move_to_downloads(paths: list[Path]) -> list[Path]:
    """No overwriting: on a clash a video and its subs get the same " (1)" so players still pair them."""
    media_stems = [p.stem for p in paths if p.suffix.lower() not in config.SUBTITLE_EXTS]
    families: dict[str, list[Path]] = {}
    for p in paths:
        base = max((s for s in media_stems if p.name.startswith(s + ".")), key=len, default=p.stem)
        families.setdefault(base, []).append(p)
    moved = []
    for base, members in families.items():
        renamed = lambda p, n: config.DOWNLOADS / (p.name if not n else f"{base} ({n}){p.name[len(base):]}")
        n = 0
        while any(renamed(p, n).exists() for p in members):
            n += 1
        for p in members:
            with suppress(OSError):
                shutil.move(str(p), str(renamed(p, n)))
                moved.append(renamed(p, n))
    return moved


async def publish_reused(job_id: str, source: str, variant: str, **kwargs) -> bool:
    reused = existing_download(job_id, source, variant, **kwargs)
    if reused:
        await jobs.emit(job_id, {"type": "complete", "files": [{"filename": p.name, "url": url} for p, url in reused],
                                 "reused": True})
    return bool(reused)


async def run_download(job_id: str, url: str, ytdlp_args: list[str], pick, *, source: str, variant: str,
                       lease: dict, cookie_path: Path | None, failed: str, missing: str, subtitle_warning: str = "",
                       playlist: bool = False) -> None:
    """Downloads into a private folder so parallel jobs never see each other's files."""
    job_dir = config.DOWNLOADS / f".job-{secrets.token_hex(12)}"
    job_dir.mkdir(parents=True)
    # .150B keeps CJK titles under the 255-byte filename limit
    if playlist:
        # -i: one private/removed item must not stop the rest
        scope, template = ["--yes-playlist", "-i"], "%(playlist_index)s - %(title).150B [%(id)s].%(ext)s"
    else:
        # --playlist-items 1: a pure playlist link only downloads its first entry
        scope, template = ["--no-playlist", "--playlist-items", "1"], "%(title).150B [%(id)s].%(ext)s"
    try:
        out_dir = str(job_dir).replace("%", "%%") + os.sep  # % is special in output templates
        args = [*ytdlp_args, *scope, "-o", out_dir + template,
                "--print-to-file", "after_move:filepath", out_dir + config.DONE_FILE, "--", url]
        code, lines = await run_yt_dlp(args, job_id=job_id, cookie_path=cookie_path)

        # A cancel that lands after yt-dlp already succeeded keeps the download.
        cancelled = job_id in jobs.cancel_requested and code != 0
        if job_id in jobs.cancel_requested and not cancelled:
            await jobs.emit(job_id, {"type": "warning", "message": "The download finished before it could be cancelled; it is kept."})
        # Playlists keep finished items even when cancelled or partly failed.
        kept = printed_paths(job_dir) if playlist and (cancelled or code != 0) else []
        unremuxed = (not playlist and not cancelled and code != 0 and "--remux-video" in ytdlp_args
                     and remux_failed(lines))
        in_playlist = not playlist and any("Downloading playlist:" in line for line in lines)

        if cancelled and not kept:
            return await jobs.emit(job_id, {"type": "cancelled", "message": "Download cancelled."})
        if code != 0 and not kept and not unremuxed:
            if in_playlist:
                await jobs.emit(job_id, {"type": "warning", "playlist_url": url, "message": (
                    "This link is a playlist and its first item could not be downloaded; the full playlist "
                    "download skips items that fail.")})
            return await jobs.emit(job_id, {"type": "error", "message": failed})
        if cancelled:
            await jobs.emit(job_id, {"type": "warning", "message": f"Download cancelled; the {len(kept)} finished item(s) are kept."})
        elif unremuxed:
            target = ytdlp_args[ytdlp_args.index("--remux-video") + 1].upper()
            await jobs.emit(job_id, {"type": "warning", "message": (
                f"The selected formats cannot be put into {target} without re-encoding, so the file is kept in "
                f"its original container{' and without embedded subtitles' if '--embed-subs' in ytdlp_args else ''}. "
                "MKV holds every codec.")})
        elif code != 0:
            errors = sum(line.startswith("ERROR:") for line in lines)
            await jobs.emit(job_id, {"type": "warning", "message": f"{errors or 'Some'} playlist item(s) could not be "
                                     f"downloaded (private, removed or blocked); {len(kept)} item(s) finished."})
        if in_playlist:
            await jobs.emit(job_id, {"type": "warning", "playlist_url": url,
                                     "message": "This link is a playlist; only its first item was downloaded."})
        subtitle_errors = sum("Unable to download video subtitles" in line for line in lines)
        if subtitle_warning and subtitle_errors:
            await jobs.emit(job_id, {"type": "warning", "message": subtitle_warning.format(n=subtitle_errors)})
        finals = move_to_downloads(pick(job_files(job_dir), printed_paths(job_dir)))
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

    if not finals:
        return await jobs.emit(job_id, {"type": "error", "message": missing})
    try:
        files = [{"filename": p.name,
                  "url": register_public_file(p, job_id=job_id, source_url=variant, source_key=source,
                                              reusable=not (subtitle_errors or unremuxed), **lease)}
                 for p in finals]
    except Exception:
        for p in finals:  # no row = nothing would ever clean these up
            with suppress(OSError):
                p.unlink()
        raise
    await jobs.emit(job_id, {"type": "complete", "files": files})


async def download_video(job_id: str, a: dict, url: str, lease: dict, cookie_path: Path | None) -> None:
    video_id = str(a.get("video_format_id") or "").strip()
    audio_id = str(a.get("audio_format_id") or "").strip()
    include_audio = bool(a.get("include_audio", True))
    container = str(a.get("container") or "original").lower()
    if container in config.AUTO_CONTAINERS:
        container = "original"  # one spelling, so auto/original share earlier downloads
    sub_mode = "embed" if str(a.get("subtitle_mode") or "embed") == "embed" else "srt"
    sub_langs = normalize_subtitle_languages(a.get("subtitle_lang") or [])
    subs = bool(a.get("subtitle_enabled")) and bool(sub_langs)
    if not subs:  # leftover settings must not make an identical download look new
        sub_mode, sub_langs = "embed", []
    source = source_key(url)

    if include_audio:
        audio = audio_id or "bestaudio"
        # fallbacks keep the exact video instead of silently swapping it
        selector = (f"{video_id}+{audio}/{video_id}+bestaudio/{video_id}" if video_id
                    else f"bestvideo+{audio}/bestvideo+bestaudio/best")
    else:
        selector = video_id or "bestvideo/best"
    variant = (f"{source}|video|{selector}|audio={int(include_audio)}"
               f"|audio_id={(audio_id or 'best') if include_audio else '-'}"
               f"|container={container}|subs={int(subs)}|submode={sub_mode}|lang={','.join(sorted(sub_langs))}")
    if container not in config.AUTO_CONTAINERS | config.REMUX_CONTAINERS:
        return await jobs.emit(job_id, {"type": "error", "message": f"Unsupported video container: {container}."})
    if await publish_reused(job_id, source, variant, require_media=True, **lease):
        return

    args = ["-f", selector, *container_args(container)]
    if subs:
        if sub_mode == "embed" and container not in config.AUTO_CONTAINERS | {"mp4", "webm", "mkv"}:
            return await jobs.emit(job_id, {"type": "error", "message": "Embedded subtitles require MP4, WebM, or MKV. "
                                            "Choose one of those containers or switch subtitle output to Separate .SRT."})
        args += ["-i", *subtitle_args(sub_langs, sub_mode)]  # -i: a failed track (often 429) mustn't fail the video

    def pick(files, printed):
        media = printed[0] if printed else newest_media(files)
        return [media, *(srt_files(files) if subs and sub_mode == "srt" else [])] if media else []

    await run_download(job_id, url, args, pick, source=source, variant=variant, lease=lease, cookie_path=cookie_path,
                       failed="Video download failed.",
                       missing="Download finished but the output video file could not be found.",
                       subtitle_warning="Some subtitle tracks failed (often HTTP 429 from YouTube), but the video "
                                        "download continued. {n} subtitle error(s).")


async def download_audio(job_id: str, a: dict, url: str, lease: dict, cookie_path: Path | None) -> None:
    selector = str(a.get("audio_source") or a.get("audio_format_id") or "").strip() or "bestaudio/best"
    target = str(a.get("audio_container") or "original").lower()
    if target not in config.AUTO_CONTAINERS | config.AUDIO_FORMATS:
        return await jobs.emit(job_id, {"type": "error", "message": f"Unsupported audio container: {target}."})
    if target in config.AUTO_CONTAINERS:
        target = "original"
    source = source_key(url)
    variant = f"{source}|audio|{selector}|container={target}"
    if await publish_reused(job_id, source, variant, require_media=True, **lease):
        return
    args = ["-f", selector] + ([] if target in config.AUTO_CONTAINERS else ["-x", "--audio-format", target])
    await run_download(job_id, url, args,
                       lambda files, printed: printed[:1] or ([m] if (m := newest_media(files)) else []),
                       source=source, variant=variant, lease=lease, cookie_path=cookie_path,
                       failed="Audio download failed.",
                       missing="Audio download finished but the output file could not be found.")


async def download_subtitle(job_id: str, a: dict, url: str, lease: dict, cookie_path: Path | None) -> None:
    langs = normalize_subtitle_languages(a.get("subtitle_lang"))
    if not langs:
        return await jobs.emit(job_id, {"type": "error", "message": "Select at least one subtitle language first."})
    source = source_key(url)
    variant = f"{source}|subtitle|lang={','.join(sorted(langs))}|srt"
    if await publish_reused(job_id, source, variant, require_media=False, **lease):
        return
    await run_download(job_id, url, ["-i", *subtitle_args(langs, "srt"), "--skip-download"],
                       lambda files, printed: srt_files(files),
                       source=source, variant=variant, lease=lease, cookie_path=cookie_path,
                       failed="Subtitle download failed.",
                       missing="Subtitle download finished but no .srt file could be found.",
                       subtitle_warning="Some selected subtitle tracks failed (often HTTP 429 from YouTube); "
                                        "successful tracks are still returned. {n} subtitle error(s).")


async def download_playlist(job_id: str, a: dict, url: str, lease: dict, cookie_path: Path | None) -> None:
    """Never reused, playlists change."""
    source = source_key(url)
    if a.get("audio_only"):
        container = str(a.get("audio_container") or "original").lower()
        if container not in config.AUTO_CONTAINERS | config.AUDIO_FORMATS:
            return await jobs.emit(job_id, {"type": "error", "message": f"Unsupported audio container: {container}."})
        args = ["-f", "bestaudio/best"] + ([] if container in config.AUTO_CONTAINERS else ["-x", "--audio-format", container])
    else:
        container = str(a.get("container") or "original").lower()
        if container not in config.AUTO_CONTAINERS | config.REMUX_CONTAINERS:
            return await jobs.emit(job_id, {"type": "error", "message": f"Unsupported video container: {container}."})
        args = ["-f", "bestvideo+bestaudio/best", *container_args(container)]
    kind = "audio" if a.get("audio_only") else "video"
    await run_download(job_id, url, args,
                       lambda files, printed: printed or [p for p in files if p.suffix.lower() not in config.SUBTITLE_EXTS],
                       source=source, variant=f"{source}|playlist|{kind}|container={container}", lease=lease,
                       cookie_path=cookie_path, playlist=True,
                       failed="Playlist download failed.",
                       missing="Playlist download finished but no files could be found.")


DOWNLOADERS = {"download_video": download_video, "download_audio": download_audio,
               "download_subtitle": download_subtitle, "download_playlist": download_playlist}


async def run_download_job(job_id: str, action: str, args: dict, user_ip: str) -> None:
    jobs.running.add(job_id)
    jobs.update(job_id, status="running", current_message="Starting download...")
    lease = {"user_ip": user_ip, "access_duration": str(args.get("access_duration") or "60"),
             "file_retention": str(args.get("file_retention") or "permanent")}
    try:
        # otherwise a bad value silently means "forever" or one hour
        for name in ("access_duration", "file_retention"):
            if not config.DURATION_RE.fullmatch(lease[name]):
                return await jobs.emit(job_id, {"type": "error",
                                                "message": f"Unsupported {name.replace('_', ' ')}: {lease[name]}."})
        with cookie_file(args) as cookie_path:
            await DOWNLOADERS[action](job_id, args, str(args.get("url") or "").strip(), lease, cookie_path)
    except asyncio.CancelledError:  # only server shutdown cancels a job
        message = "The server stopped while this download was running."
        jobs.flush(job_id, status="failed", current_message=message, error=message)
        raise
    except Exception as exc:
        await jobs.emit(job_id, {"type": "error", "message": str(exc)})
    finally:
        jobs.finished(job_id)
