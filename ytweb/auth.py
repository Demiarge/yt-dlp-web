"""Password, login lockout and sessions."""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import deque
from contextlib import suppress
from urllib.parse import urlparse

from fastapi import Request, WebSocket

from . import config
from .tasks import spawn

# OWASP scrypt params (~64 MiB per hash). Old params get rehashed on the next good login.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 2
SCRYPT_MAX_BLOCK_BYTES = 256 * 1024 * 1024  # refuse hand-edited params that need more RAM
MAX_PASSWORD_LENGTH = 1024
MAX_LOGIN_BODY = 16 * 1024  # 1024 chars even if all \u-escaped
BLOCK_SECONDS = config.BLOCK_MINUTES * 60
FAILURE_WINDOW_SECONDS = config.FAILURE_WINDOW_MINUTES * 60
SESSION_SECONDS = config.SESSION_DAYS * 24 * 60 * 60

LOOPBACK_IPS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
AUTH_DAMAGED_MESSAGE = (f"{config.AUTH_PATH.name} is damaged. "
                        "Delete it and run `python server.py` again to set a new password.")


class AuthFileDamaged(Exception):
    pass


def scrypt_hash(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    maxmem = 128 * r * (n + p + 2) + 1024 * 1024
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=maxmem, dklen=64)


def write_auth(password: str) -> dict:
    salt = secrets.token_bytes(16)
    auth = {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": salt,
            "hash": scrypt_hash(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)}
    record = {"algorithm": "scrypt", **auth, "salt": salt.hex(), "hash": auth["hash"].hex()}
    tmp = config.AUTH_PATH.with_suffix(".json.tmp")  # atomic write
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, config.AUTH_PATH)
    return auth


def load_auth() -> dict:
    try:
        data = json.loads(config.AUTH_PATH.read_text(encoding="utf-8"))
        n, r, p, algorithm = data["n"], data["r"], data["p"], data.get("algorithm")
        salt, expected = bytes.fromhex(data["salt"]), bytes.fromhex(data["hash"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AuthFileDamaged from exc
    if not (algorithm == "scrypt" and all(type(v) is int for v in (n, r, p))
            and 2 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r and 1 <= p <= 16
            and 128 * r * (n + p + 2) <= SCRYPT_MAX_BLOCK_BYTES and len(salt) >= 16 and len(expected) == 64):
        raise AuthFileDamaged
    return {"n": n, "r": r, "p": p, "salt": salt, "hash": expected}


def setup_password() -> None:
    print(f"No {config.AUTH_PATH.name} found. Set a password for yt-dlp Web.")
    while True:
        password = getpass.getpass("New password: ")
        if not password:
            print("Password cannot be empty.")
        elif getpass.getpass("Confirm password: ") != password:
            print("Passwords do not match. Try again.")
        else:
            break
    write_auth(password)
    print(f"Password saved to {config.AUTH_PATH.name}.")


class LoginGuard:
    """Checks passwords, rate-limits wrong ones per IP and globally."""

    def __init__(self) -> None:
        self.auth: dict | None = None
        self.verified: set[str] = set()  # sha256 of the right password, skips scrypt on re-login
        self.failures: dict[str, tuple[int, float]] = {}  # ip -> (count, first failure time)
        self.blocked_until: dict[str, float] = {}
        self.recent_failures: deque[float] = deque()  # all non-exempt addresses
        self.global_blocked_until = 0.0
        self.locks: dict[str, asyncio.Lock] = {}  # one check at a time per ip
        self.pending_hashes = 0
        self.scrypt_slots = asyncio.Semaphore(2)

    async def _verify(self, password: str) -> bool:
        auth = self.auth
        if auth is None:
            return False
        async with self.scrypt_slots:
            actual = await asyncio.to_thread(scrypt_hash, password, auth["salt"], auth["n"], auth["r"], auth["p"])
            if not hmac.compare_digest(actual, auth["hash"]):
                return False
            if (auth["n"], auth["r"], auth["p"]) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
                with suppress(OSError):
                    self.auth = await asyncio.to_thread(write_auth, password)
            return True

    def blocked_seconds(self, ip: str) -> int:
        now = time.time()
        if self.blocked_until.get(ip, now) <= now:
            self.blocked_until.pop(ip, None)
        until = max(self.blocked_until.get(ip, 0.0), self.global_blocked_until)
        return int(until - now) + 1 if now < until else 0

    async def check(self, ip: str, password: str, exempt: bool = False) -> tuple[str, int]:
        """-> ("ok" | "wrong" | "blocked" | "busy", retry_after). Exempt = localhost, never blocked."""
        if not exempt and (retry := self.blocked_seconds(ip)):
            return "blocked", retry
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        if digest in self.verified:
            self.failures.pop(ip, None)
            return "ok", 0

        # re-check inside the lock, or a burst of parallel guesses would all get hashed
        async with self.locks.setdefault(ip, asyncio.Lock()):
            if not exempt and (retry := self.blocked_seconds(ip)):
                return "blocked", retry
            if digest not in self.verified:
                if self.pending_hashes >= config.MAX_PENDING_HASHES:
                    return "busy", 2
                self.pending_hashes += 1
                try:
                    if await self._verify(password):
                        self.verified.add(digest)
                finally:
                    self.pending_hashes -= 1
            if digest in self.verified:
                self.failures.pop(ip, None)
                return "ok", 0

            if exempt:
                self.failures.pop(ip, None)
                return "wrong", 0

            now = time.time()
            if config.MAX_FAILED_PASSWORDS_TOTAL:
                self.recent_failures.append(now)
                while now - self.recent_failures[0] > FAILURE_WINDOW_SECONDS:
                    self.recent_failures.popleft()
                if len(self.recent_failures) >= config.MAX_FAILED_PASSWORDS_TOTAL:
                    self.recent_failures.clear()
                    self.global_blocked_until = now + BLOCK_SECONDS
                    self.failures.pop(ip, None)
                    return "blocked", BLOCK_SECONDS
            failures, first_failed = self.failures.get(ip, (0, now))
            if now - first_failed > FAILURE_WINDOW_SECONDS:
                failures, first_failed = 0, now
            failures += 1
            if failures >= config.MAX_FAILED_PASSWORDS:
                self.failures.pop(ip, None)
                self.blocked_until[ip] = now + BLOCK_SECONDS
                return "blocked", BLOCK_SECONDS
            self.failures[ip] = (failures, first_failed)
            return "wrong", 0

    def prune(self) -> None:
        now = time.time()
        for ip in [ip for ip, until in self.blocked_until.items() if now >= until]:
            del self.blocked_until[ip]
        for ip in [ip for ip, (_, first) in self.failures.items() if now - first > FAILURE_WINDOW_SECONDS]:
            del self.failures[ip]
        # a released lock can still have a woken waiter that hasn't run yet
        for ip in [ip for ip, lock in self.locks.items() if not lock.locked() and not getattr(lock, "_waiters", None)]:
            del self.locks[ip]


class Sessions:
    """Login token -> expiry. In memory only, a restart logs everyone out."""

    def __init__(self) -> None:
        self.expiry: dict[str, float] = {}
        self.sockets: dict[str, set[WebSocket]] = {}  # live connections per login, closed on logout

    def new(self) -> str:
        token = secrets.token_urlsafe(32)
        self.expiry[token] = time.time() + SESSION_SECONDS
        return token

    def valid(self, token: str | None) -> bool:
        expires = self.expiry.get(token or "")
        return expires is not None and time.time() < expires

    async def end(self, token: str | None) -> None:
        """4001 makes the page show the login form."""
        self.expiry.pop(token or "", None)
        for websocket in self.sockets.pop(token or "", set()):
            with suppress(Exception):
                await websocket.close(code=4001)

    def add_socket(self, token: str, websocket: WebSocket) -> None:
        self.sockets.setdefault(token, set()).add(websocket)

    def remove_socket(self, token: str, websocket: WebSocket) -> None:
        sockets = self.sockets.get(token)
        if sockets is not None:
            sockets.discard(websocket)
            if not sockets:
                self.sockets.pop(token, None)

    def prune(self) -> None:
        now = time.time()
        for token in [t for t, expires in self.expiry.items() if now >= expires]:
            spawn(self.end(token))


guard = LoginGuard()
sessions = Sessions()


def prune() -> None:
    guard.prune()
    sessions.prune()


def real_client_ip(conn: Request | WebSocket) -> str:
    # uvicorn already applied X-Forwarded-For when proxy headers are on
    return conn.client.host if conn.client else "unknown"


def request_host_name(conn: Request | WebSocket) -> str:
    try:
        return urlparse("//" + conn.headers.get("host", "")).hostname or ""
    except ValueError:
        return ""


def lockout_exempt(conn: Request | WebSocket, ip: str) -> bool:
    # check Host too: a DNS-rebinding page also connects from 127.0.0.1
    return config.NEVER_BLOCK_LOCAL and ip in LOOPBACK_IPS and request_host_name(conn) in LOOPBACK_HOSTS


def same_origin(conn: Request | WebSocket) -> bool:
    """CSRF check on top of SameSite=Strict."""
    origin = conn.headers.get("origin")
    if not origin:
        return True  # not a browser; still needs the cookie
    origin_host = urlparse(origin).netloc.lower()
    hosts = {conn.headers.get(h, "").lower() for h in ("host", "x-forwarded-host")} - {""}
    return origin_host in hosts
