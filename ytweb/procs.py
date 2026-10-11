"""Making sure yt-dlp / ffmpeg never outlive the server, however it dies."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from contextlib import suppress


async def kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Kill yt-dlp together with its ffmpeg children."""
    if process.returncode is not None:
        return
    with suppress(OSError):
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/F", "/T", "/PID", str(process.pid),
                                                          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)


def new_session_kwargs() -> dict:
    """POSIX: own process group, so killpg gets ffmpeg too."""
    return {} if os.name == "nt" else {"start_new_session": True}


_server_job = None  # handle must stay open while the server runs


def _tie_children_windows() -> bool:
    """Job object with KILL_ON_JOB_CLOSE: yt-dlp and ffmpeg die with the server, even on taskkill /F."""
    global _server_job
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))  # 9 = extended limits
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        return False
    _server_job = job
    return True


# POSIX watchdog: kills the listed process groups once the server's pipe closes (even on kill -9).
WATCHDOG_CODE = r"""
import os, signal, sys
for name in ("SIGINT", "SIGHUP", "SIGTERM"):
    signal.signal(getattr(signal, name), signal.SIG_IGN)
groups = set()
for line in sys.stdin:
    action, _, pgid = line.strip().partition(" ")
    if pgid.isdigit():
        (groups.add if action == "add" else groups.discard)(int(pgid))
for pgid in groups:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
"""
_watchdog: subprocess.Popen | None = None


def _start_watchdog() -> bool:
    global _watchdog
    try:
        # own session: Ctrl+C / closed terminal must not reach it
        _watchdog = subprocess.Popen([sys.executable, "-c", WATCHDOG_CODE], stdin=subprocess.PIPE,
                                     text=True, start_new_session=True)
    except OSError:
        return False
    return True


def watch_process_group(pgid: int, running: bool) -> None:
    if os.name == "nt" or _watchdog is None or _watchdog.stdin is None:
        return
    with suppress(OSError, ValueError):
        _watchdog.stdin.write(f"{'add' if running else 'del'} {pgid}\n")
        _watchdog.stdin.flush()


def tie_children_to_lifetime() -> bool:
    return _tie_children_windows() if os.name == "nt" else _start_watchdog()
