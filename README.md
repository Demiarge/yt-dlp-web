# yt-dlp Web

A small, password-protected web interface for [yt-dlp](https://github.com/yt-dlp/yt-dlp).

Paste a link, choose what you want, and get a download link when it finishes.

## Features

- Choose the exact video and audio formats
- Download subtitles
- Download whole playlists
- Get a download link for each finished job, with an optional expiry
- Use cookie files for videos that need you to be signed in
- Log in with a password

## Requirements

| Requirement | Purpose |
|---|---|
| Python 3.11 or newer | Runs the app |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Does the downloading |
| [FFmpeg](https://ffmpeg.org/) | Merges video and audio, converts audio, embeds subtitles |
| `fastapi`, `uvicorn`, `websockets` | Python packages, installed from `requirements.txt` |

## Installation

### 1. Install yt-dlp and FFmpeg

Download and install both tools before you run the app. The installation instructions for yt-dlp are on its GitHub page:
https://github.com/yt-dlp/yt-dlp#installation

Some package managers install FFmpeg along with yt-dlp. On Windows, this winget command installs both:

```powershell
winget install -e --id yt-dlp.yt-dlp
```

If your package manager doesn't include FFmpeg, install it separately from https://ffmpeg.org/.

### 2. Set up the app

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Start the server

```
python server.py
```

The first time you start it, the app asks you to set a password. Only a hash of the password is saved, in `auth.json`.

Then open http://127.0.0.1:8765 in your browser and log in.

### Resetting the password

To reset the password, stop the server and delete `auth.json` (in the data folder, which is the project folder by default). The next time you start the server, it asks you to set a new password.

## Using cookies

Some videos can only be watched when you're signed in, such as members-only or private videos. To download them, export your browser cookies in Netscape format. Put the files in the `cookies/` folder, and give each one a name that ends in `cookies.txt` (for example `youtube-cookies.txt`).

The files show up in the **Cookies** dropdown in the interface.

You can also paste cookie data straight into the cookie box in the interface. Pasted cookies are used instead of the saved file.

## Configuration

You can set everything except the host and port with an environment variable. You can also edit the line in the file directly.

| Setting | File | Variable | Default |
|---|---|---|---|
| yt-dlp executable | `ytweb/config.py` | `YTDLP` (env `YTDLP_BIN`) | `yt-dlp` found on PATH |
| FFmpeg folder | `ytweb/config.py` | `FFMPEG_DIR` (env `FFMPEG_DIR`) | empty, which uses FFmpeg from PATH |
| Data folder (downloads, cookies, databases, `auth.json`) | `ytweb/config.py` | `DATA` (env `YTDL_WEB_DATA`) | the project folder |
| Auto-update yt-dlp on start | `ytweb/web.py` | env `YTDLP_AUTO_UPDATE=1` | off |
| Host and port | `server.py` | `host=` / `port=` in `uvicorn.run(...)` | `127.0.0.1`, `8765` |

`FFMPEG_DIR` must be the folder that contains `ffmpeg`, not the path to the executable.

### Example: environment variables (PowerShell)

```powershell
$env:YTDLP_BIN = "C:\tools\yt-dlp.exe"
$env:FFMPEG_DIR = "C:\tools\ffmpeg\bin"
python server.py
```

### Example: editing `ytweb/config.py`

```python
YTDLP = os.environ.get("YTDLP_BIN") or r"C:\tools\yt-dlp.exe"
FFMPEG_DIR = os.environ.get("FFMPEG_DIR", r"C:\tools\ffmpeg\bin")
```

### Security settings

These are set at the top of `ytweb/config.py`:

| Setting | Variable | Default |
|---|---|---|
| How long a login lasts | `SESSION_DAYS` | `7` (logins also end when the server restarts) |
| Wrong passwords before an IP is blocked | `MAX_FAILED_PASSWORDS` within `FAILURE_WINDOW_MINUTES` | `5` within `15` minutes |
| How long a block lasts | `BLOCK_MINUTES` | `5` |
| Wrong passwords from all IPs before everyone is blocked | `MAX_FAILED_PASSWORDS_TOTAL` | `30` (`0` turns it off) |
| Never block localhost | `NEVER_BLOCK_LOCAL` | `True` |
| Trust proxy headers from localhost | `TRUST_PROXY_HEADERS` | `True` |
| Host names the server answers to | `ALLOWED_HOSTS` | empty, which allows any |

The comments next to each setting in `ytweb/config.py` explain when to change it. That matters most if you run the app behind a reverse proxy or a tunnel.
