"""FastAPI app: page, login, public files and the websocket the page talks over."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import auth, config
from .auth import guard, sessions
from .cookies import cookie_file, list_cookie_files
from .db import close_all, downloads_db, jobs_db
from .downloads import DOWNLOADERS, run_download_job
from .jobs import registry as jobs
from .leases import cleanup_expired, find_lease, is_web_url, lease_info
from .procs import kill_process_tree, tie_children_to_lifetime
from .tasks import cancel_all, spawn
from .ytdlp import FETCHERS, check_ytdlp, fetch_info, in_youtube_list, is_playlist, playlist_message, update_ytdlp


async def every(seconds: int, *tasks) -> None:
    while True:
        for task in tasks:
            try:
                task()
            except Exception as exc:  # keep looping, but don't hide a cleanup that keeps failing
                print(f"{task.__name__} failed: {exc!r}", file=sys.stderr)
        await asyncio.sleep(seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.AUTH_PATH.exists():
        raise RuntimeError(f"No {config.AUTH_PATH.name} found. Run `python server.py` to set a password.")
    try:
        guard.auth = auth.load_auth()
    except auth.AuthFileDamaged:
        raise RuntimeError(auth.AUTH_DAMAGED_MESSAGE) from None
    config.DOWNLOADS.mkdir(parents=True, exist_ok=True)
    config.COOKIES.mkdir(parents=True, exist_ok=True)
    downloads_db.connection()
    jobs_db.connection()
    if not tie_children_to_lifetime():
        print("Note: could not tie yt-dlp processes to the server; a force-killed server may leave downloads running.")
    if os.environ.get("YTDLP_AUTO_UPDATE") == "1":
        await update_ytdlp()
    print(f"Using yt-dlp {await check_ytdlp()} from {config.YTDLP}")

    # Leftovers of a killed server: half-done job folders, cookie copies (may hold logins), auth temp file.
    for leftover in config.DOWNLOADS.glob(".job-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    for leftover in [*config.COOKIES.glob(f"{config.COOKIE_RUN_PREFIX}*"), config.AUTH_PATH.with_suffix(".json.tmp")]:
        with suppress(OSError):
            leftover.unlink()
    cleanup_expired()  # sync DB with downloads/ before anything new starts

    loop = asyncio.create_task(every(60, auth.prune, cleanup_expired, jobs.cleanup))
    try:
        yield
    finally:
        loop.cancel()
        with suppress(asyncio.CancelledError):
            await loop
        await cancel_all()  # each job kills its own yt-dlp on the way out
        close_all()


app = FastAPI(lifespan=lifespan)
if config.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)

# No login needed. /public links are guarded by their random token, so VLC can open them.
PUBLIC_PATHS = {"/", "/session", "/login", "/logout", "/health", "/favicon.ico"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/public/") or sessions.valid(request.cookies.get(config.SESSION_COOKIE)):
        return await call_next(request)
    return JSONResponse({"error": "Login required."}, status_code=401)


@app.get("/")
async def index():
    return FileResponse(config.INDEX)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/session")
async def session_state(request: Request):
    # server_time: the page judges link expiry by our clock, not the device's
    return {"logged_in": sessions.valid(request.cookies.get(config.SESSION_COOKIE)),
            "session_days": config.SESSION_DAYS, "server_time": time.time()}


def cookie_flags(request: Request) -> dict:
    # scheme is https behind a TLS proxy too (uvicorn applies X-Forwarded-Proto)
    return {"path": "/", "httponly": True, "samesite": "strict", "secure": request.url.scheme == "https"}


async def read_limited_body(request: Request, limit: int) -> bytes | None:
    """None once the body is over `limit`, without buffering the rest."""
    with suppress(ValueError):
        if int(request.headers.get("content-length") or 0) > limit:
            return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body


def login_failure_response(state: str, retry_after: int) -> Response:
    if state == "blocked":
        return JSONResponse({"error": f"Too many wrong passwords. Try again in {retry_after} seconds.",
                             "retry_after": retry_after}, status_code=429, headers={"Retry-After": str(retry_after)})
    if state == "busy":
        return JSONResponse({"error": "The server is busy checking other passwords. Try again in a moment.",
                             "retry_after": retry_after}, status_code=503, headers={"Retry-After": str(retry_after)})
    return JSONResponse({"error": "Wrong password."}, status_code=401)


@app.post("/login")
async def login(request: Request):
    # JSON only: a cross-site form can't send that without a CORS preflight
    if not auth.same_origin(request) or "application/json" not in request.headers.get("content-type", ""):
        return JSONResponse({"error": "Bad request."}, status_code=400)
    body = await read_limited_body(request, auth.MAX_LOGIN_BODY)
    if body is None:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    try:
        data = json.loads(body)
        password = data.get("password") if isinstance(data, dict) else None
        if isinstance(password, str):
            password.encode("utf-8")  # lone surrogates are valid JSON but not a password
    except (ValueError, RecursionError):
        password = None
    if not isinstance(password, str) or not password or len(password) > auth.MAX_PASSWORD_LENGTH:
        return JSONResponse({"error": "Enter the password."}, status_code=400)
    ip = auth.real_client_ip(request)
    state, retry_after = await guard.check(ip, password, auth.lockout_exempt(request, ip))
    if state != "ok":
        return login_failure_response(state, retry_after)
    response = JSONResponse({"ok": True, "session_days": config.SESSION_DAYS})
    response.set_cookie(config.SESSION_COOKIE, sessions.new(), max_age=auth.SESSION_SECONDS, **cookie_flags(request))
    return response


@app.post("/logout")
async def logout(request: Request):
    if not auth.same_origin(request):
        return JSONResponse({"error": "Bad request."}, status_code=400)
    await sessions.end(request.cookies.get(config.SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(config.SESSION_COOKIE, **cookie_flags(request))
    return response


@app.get("/cookies")
async def cookies_list():
    return list_cookie_files()


# HEAD too: some players ask for the size first.
@app.api_route("/public/{token}/{filename:path}", methods=["GET", "HEAD"])
async def public_file(token: str, filename: str):
    # just this row, no full cleanup: VLC sends a request per seek
    row = find_lease(token)
    if not row:
        return PlainTextResponse("Link expired or not found.", status_code=410)
    if lease_info(row)["expired"]:
        return PlainTextResponse("Link expired.", status_code=410)
    path = Path(row["file_path"]).resolve()
    if not path.is_relative_to(config.DOWNLOADS.resolve()):
        raise HTTPException(status_code=404, detail="File not found.")
    if not path.is_file() or path.name != Path(filename).name:
        return PlainTextResponse("Link expired. The downloaded file is no longer available.", status_code=410)
    return FileResponse(path, filename=path.name)


async def handle_fetch(websocket: WebSocket, action: str, url: str, data: dict) -> None:
    kind, what, build = FETCHERS[action]
    # lets the page match answers and errors to its fetch and URL
    tag = {"fetch": kind, "request_url": url}
    try:
        with cookie_file(data) as cookie_path:
            if not (info := await fetch_info(websocket, url, cookie_path, what, tag)):
                return
            if not is_playlist(info):
                message = build(info, url)
                if kind == "formats" and in_youtube_list(url):
                    message["playlist"] = {"title": "", "count": None, "url": url}
                await websocket.send_json({**message, "request_url": url})
            elif kind == "formats":
                await websocket.send_json({**playlist_message(info, url), "request_url": url})
            else:
                await websocket.send_json({"type": "error", **tag, "message": (
                    f"This link is a playlist, so there is no {what} for it. Open a single video from it.")})
    except Exception as exc:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(exc), **tag})


async def handle_message(websocket: WebSocket, data: dict, user_ip: str, owned_jobs: set[str]) -> None:
    send = websocket.send_json
    action = str(data.get("action") or "")
    url = str(data.get("url") or "").strip()
    job_id = str(data.get("job_id") or "").strip()

    if action == "resume":
        snapshot = jobs.snapshot(job_id) if job_id else None
        if not snapshot:
            return await send({"type": "session_expired", "job_id": job_id})
        jobs.attach(job_id, websocket)
        owned_jobs.add(job_id)
        await send({"type": "session_restored", "job_id": job_id, **snapshot, "cancellable": job_id in jobs.running})
    elif action == "cancel":
        if job_id not in jobs.running:
            return await send({"type": "cancel_unavailable", "job_id": job_id, "message": "This download is no longer running."})
        jobs.cancel_requested.add(job_id)
        if process := jobs.processes.get(job_id):
            await kill_process_tree(process)
    elif action not in DOWNLOADERS and action not in FETCHERS:
        await send({"type": "error", "message": f"Unknown action: {action}"})
    elif not url:
        await send({"type": "error", "message": "URL is required."})
    elif not is_web_url(url):
        await send({"type": "error", "message": "Only http:// and https:// links are allowed."})
    elif action in DOWNLOADERS:
        title = str(data.get("media_title") or "").strip()
        job_id = jobs.create(action, url, user_ip, title, job_id)
        jobs.running.add(job_id)  # before any await, so a resume never sees it as orphaned
        jobs.attach(job_id, websocket)
        owned_jobs.add(job_id)
        try:
            await send({"type": "job_started", "job_id": job_id, "status": "running", "source_url": url, "media_title": title})
        finally:
            spawn(run_download_job(job_id, action, dict(data), user_ip))  # outlives the socket
    else:
        spawn(handle_fetch(websocket, action, url, dict(data)))  # 5-15 s, don't block cancel etc.


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    user_ip = auth.real_client_ip(websocket)
    login = websocket.cookies.get(config.SESSION_COOKIE)
    if not auth.same_origin(websocket) or not sessions.valid(login):
        return await websocket.close(code=1008)
    await websocket.accept()
    if not sessions.valid(login):  # logged out during the handshake
        return await websocket.close(code=4001)
    sessions.add_socket(login, websocket)
    owned_jobs: set[str] = set()

    try:
        while True:
            try:
                data = json.loads(await websocket.receive_text())
                json.dumps(data, ensure_ascii=False).encode("utf-8")  # reject lone surrogates
            except (ValueError, KeyError, RecursionError):  # KeyError: binary frame
                data = None
            if not sessions.valid(login):
                await websocket.close(code=4001)
                break
            if not isinstance(data, dict):
                await websocket.send_json({"type": "error", "message": "Expected a JSON object."})
                continue
            try:  # one bad message gets an error, the connection stays
                await handle_message(websocket, data, user_ip, owned_jobs)
            except WebSocketDisconnect:
                raise
            except Exception as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        for job_id in owned_jobs:
            jobs.detach(job_id, websocket)
        sessions.remove_socket(login, websocket)
