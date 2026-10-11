"""Cookie files for yt-dlp. Each run gets its own copy, changes get merged back."""
from __future__ import annotations

import os
import secrets
from contextlib import contextmanager, suppress
from pathlib import Path

from . import config


def list_cookie_files() -> list[str]:
    return sorted((p.name for p in config.COOKIES.iterdir() if p.is_file() and p.name.lower().endswith("cookies.txt")),
                  key=str.lower)


def parse_cookies(text: str) -> tuple[list[str], dict[tuple[str, str, str], str]]:
    """Netscape cookie file -> (comment lines, {(domain, path, name): line})."""
    comments, cookies = [], {}
    for line in text.splitlines():
        fields = line.removeprefix("#HttpOnly_").split("\t")
        if len(fields) == 7 and (not line.startswith("#") or line.startswith("#HttpOnly_")):
            cookies[(fields[0], fields[2], fields[5])] = line
        elif line.strip():
            comments.append(line)
    return comments, cookies


def merge_cookie_changes(current: str, before: str, after: str) -> str | None:
    """Merge one run's changes in cookie by cookie, so parallel runs don't clobber each other. None = don't write."""
    _, base = parse_cookies(before)
    _, run = parse_cookies(after)
    if not run or not after.endswith("\n"):
        return None  # empty or cut off (yt-dlp killed mid-save); merging would drop cookies
    comments, merged = parse_cookies(current)
    for key, line in run.items():
        if base.get(key) != line:
            merged[key] = line
    for key in base.keys() - run.keys():  # deleted / expired during the run
        merged.pop(key, None)
    return "\n".join((comments or ["# Netscape HTTP Cookie File"]) + list(merged.values())) + "\n"


@contextmanager
def cookie_file(data: dict):
    """Yields this run's cookie file or None. Pasted cookies beat the saved file.
    yt-dlp rewrites --cookies on exit, hence the copy."""
    custom = str(data.get("custom_cookies") or "")
    name = str(data.get("cookie_file") or "")
    saved: Path | None = config.COOKIES / name
    if custom.strip():
        content, saved = custom.encode("utf-8"), None
    elif name and Path(name).name == name and name.lower().endswith("cookies.txt") and saved.is_file():
        content = saved.read_bytes()
    else:
        yield None
        return
    path = config.COOKIES / f"{config.COOKIE_RUN_PREFIX}{secrets.token_hex(12)}.txt"
    path.write_bytes(content)
    try:
        yield path
    finally:
        with suppress(OSError):
            updated = path.read_bytes()
            if saved is not None and updated != content:
                text = lambda b: b.decode("utf-8", "surrogateescape")  # round-trips non-UTF-8 bytes
                merged = merge_cookie_changes(text(saved.read_bytes()), text(content), text(updated))
                if merged is not None:
                    tmp = saved.with_name(f"{config.COOKIE_RUN_PREFIX}{secrets.token_hex(12)}.tmp")
                    tmp.write_bytes(merged.encode("utf-8", "surrogateescape"))
                    os.replace(tmp, saved)
        with suppress(OSError):
            path.unlink()
