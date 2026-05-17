"""
Utilities for detecting and launching Quicken on Windows.

Usage:
    from app.quicken_launcher import open_in_quicken
    open_in_quicken(qfx_path, log=my_log_fn)
"""
from __future__ import annotations

import os
import subprocess
import winreg
from typing import Callable

# os.startfile must be called from the main thread on some Python builds.
# We use ShellExecute via ctypes so it is safe from any thread.
import ctypes
import ctypes.wintypes

# Fallback install locations if registry lookup fails
_KNOWN_PATHS = [
    r"C:\Program Files (x86)\Quicken\qw.exe",
    r"C:\Program Files\Quicken\qw.exe",
    r"C:\Program Files (x86)\Intuit\Quicken\qw.exe",
    r"C:\Program Files\Intuit\Quicken\qw.exe",
]


def find_quicken_exe() -> str | None:
    """Return the full path to qw.exe, or None if Quicken is not installed."""
    # 1. App Paths registry key (most reliable)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for subkey in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\qw.exe",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\qw.exe",
        ):
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val and os.path.isfile(val):
                        return val
            except OSError:
                pass

    # 2. .qfx file type association: "C:\...\qw.exe" -X "%1"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, r"qfxfile\shell\open\command"
        ) as k:
            cmd, _ = winreg.QueryValueEx(k, "")
            # Strip leading quote, then grab everything up to the closing quote
            exe = cmd.lstrip('"').split('"')[0].strip()
            if os.path.isfile(exe):
                return exe
    except OSError:
        pass

    # 3. Known install paths
    for p in _KNOWN_PATHS:
        if os.path.isfile(p):
            return p

    return None


def is_quicken_running() -> bool:
    """Return True if qw.exe is currently in the Windows process list."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq qw.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "qw.exe" in result.stdout.lower()
    except Exception:
        return False


def _shell_open(path: str) -> None:
    """
    Open *path* via Windows ShellExecuteW — safe from any thread, equivalent
    to double-clicking the file in Explorer.  This goes through the registered
    .qfx file association and respects Quicken's single-instance DDE handler.
    """
    shell32 = ctypes.windll.shell32
    # ShellExecuteW(hwnd, verb, file, params, directory, show)
    ret = shell32.ShellExecuteW(None, "open", path, None, None, 1)
    if ret <= 32:
        raise OSError(f"ShellExecuteW returned {ret} for {path!r}")


def open_in_quicken(
    qfx_path: str,
    log: Callable[[str], None] | None = None,
) -> None:
    """
    Open *qfx_path* in Quicken via Windows ShellExecute (same as
    double-clicking the file in Explorer).

    - If Quicken is not running it is launched and the file is imported.
    - If Quicken is already running, Windows passes the file to the existing
      instance through Quicken's DDE/single-instance handler.

    Errors are logged rather than raised so a failed launch never hides a
    successful conversion result.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    # Normalise to Windows backslash path — some shell handlers are fussy.
    qfx_path = os.path.normpath(qfx_path)

    if not os.path.isfile(qfx_path):
        _log(f"WARN  QFX file not found: {qfx_path}")
        return

    if find_quicken_exe() is None:
        _log("WARN  Quicken not found — open the QFX file manually")
        return

    already_running = is_quicken_running()
    if already_running:
        _log("Quicken already running — importing QFX...")
    else:
        _log("Launching Quicken...")

    try:
        _shell_open(qfx_path)
    except Exception as exc:
        _log(f"WARN  Could not open QFX in Quicken: {exc}")
