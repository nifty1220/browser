"""
BrowserGuard — launch isolated Chrome/Brave sessions with proxy rotation
and crash recovery.  Single file, stdlib only.
"""

# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------

import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 2. THEME — all colours for light and dark mode
# ---------------------------------------------------------------------------

THEME: Dict[str, Dict[str, str]] = {
    "light": {
        "bg":        "#F2F2F7",
        "card":      "#FFFFFF",
        "surface2":  "#F2F2F7",
        "accent":    "#007AFF",
        "success":   "#34C759",
        "danger":    "#FF3B30",
        "warning":   "#FF9500",
        "text":      "#000000",
        "text2":     "#6C6C70",
        "text3":     "#AEAEB2",
        "separator": "#C6C6C8",
        "toolbar":   "#FFFFFF",
    },
    "dark": {
        "bg":        "#1C1C1E",
        "card":      "#2C2C2E",
        "surface2":  "#3A3A3C",
        "accent":    "#0A84FF",
        "success":   "#30D158",
        "danger":    "#FF453A",
        "warning":   "#FF9F0A",
        "text":      "#FFFFFF",
        "text2":     "#8E8E93",
        "text3":     "#48484A",
        "separator": "#38383A",
        "toolbar":   "#2C2C2E",
    },
}

# ---------------------------------------------------------------------------
# 3. Font resolution
# ---------------------------------------------------------------------------

_F_UI:   str = "TkDefaultFont"
_F_MONO: str = "TkFixedFont"

_UI_CANDIDATES:   List[str] = ["SF Pro Display", "Helvetica Neue", "Calibri", "Arial"]
_MONO_CANDIDATES: List[str] = ["SF Mono", "Consolas", "Courier New"]


def _resolve_fonts() -> None:
    """Detect available font families and set _F_UI / _F_MONO globals.

    Must be called *after* the Tk root window is created so that
    tkinter.font.families() returns the real system font list.
    """
    global _F_UI, _F_MONO
    try:
        available = set(tkfont.families())
    except Exception:
        available = set()

    for name in _UI_CANDIDATES:
        if name in available:
            _F_UI = name
            break
    else:
        _F_UI = "TkDefaultFont"

    for name in _MONO_CANDIDATES:
        if name in available:
            _F_MONO = name
            break
    else:
        _F_MONO = "TkFixedFont"

    logger.debug("Fonts resolved — UI: %r  Mono: %r", _F_UI, _F_MONO)


def f(size: int, weight: str = "normal") -> Tuple[str, int, str]:
    """Return a UI font tuple: (family, size, weight)."""
    return (_F_UI, size, weight)


def fm(size: int) -> Tuple[str, int]:
    """Return a monospace font tuple: (family, size)."""
    return (_F_MONO, size)


# ---------------------------------------------------------------------------
# 4. CONFIG — application defaults
# ---------------------------------------------------------------------------

CONFIG: Dict = {
    "browser_path":       "",
    "selected_browser":   "auto",       # "auto" | "chrome" | "brave"
    "proxy_mode":         "none",        # "none" | "single" | "rotate"
    "proxy_list":         [],
    "website_list":       [],
    "crash_restart":      True,
    "crash_max_retries":  5,
    "crash_retry_delay":  3,             # seconds between restart attempts
    "window_width":       1280,
    "window_height":      800,
    "user_agent_rotate":  True,
}

# ---------------------------------------------------------------------------
# 5. Browser discovery — find_brave() and find_chrome()
# ---------------------------------------------------------------------------

def find_brave() -> Optional[str]:
    """Return the absolute path to the Brave executable, or None if not found."""
    candidates: List[str] = []

    if sys.platform == "win32":
        _local = os.environ.get("LOCALAPPDATA", "")
        _prog  = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        _prog86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates = [
            os.path.join(_prog,   "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(_prog86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(_local,  "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            os.path.expanduser(
                "~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
            ),
        ]
    else:
        # Linux / BSD
        names = ["brave-browser", "brave-browser-stable", "brave", "brave-browser-beta"]
        for name in names:
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            "/usr/bin/brave-browser",
            "/usr/bin/brave",
            "/usr/bin/brave-browser-stable",
            "/snap/bin/brave",
            "/usr/local/bin/brave-browser",
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def find_chrome() -> Optional[str]:
    """Return the absolute path to the Google Chrome executable, or None if not found."""
    candidates: List[str] = []

    if sys.platform == "win32":
        _local = os.environ.get("LOCALAPPDATA", "")
        _prog  = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        _prog86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates = [
            os.path.join(_prog,   "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(_prog86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(_local,  "Google", "Chrome", "Application", "chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
        ]
    else:
        # Linux / BSD
        names = [
            "google-chrome", "google-chrome-stable", "google-chrome-beta",
            "chromium-browser", "chromium",
        ]
        for name in names:
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
            "/usr/local/bin/google-chrome",
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ---------------------------------------------------------------------------
# 6. normalize_proxy(s)
# ---------------------------------------------------------------------------

def normalize_proxy(s: str) -> Tuple[bool, str]:
    """Normalize a proxy string to a canonical URL.

    Accepted formats
    ----------------
    - ``host:port``
    - ``host:port:user:pass``
    - ``user:pass@host:port``
    - ``http://[user:pass@]host:port``
    - ``https://[user:pass@]host:port``
    - ``socks5://[user:pass@]host:port``
    - ``socks4://[user:pass@]host:port``

    Returns
    -------
    ``(True, normalized_url)`` on success or ``(False, error_string)`` on failure.
    Normalized form is ``scheme://user:pass@host:port`` (with auth) or
    ``scheme://host:port`` (without auth).  Plain host:port entries use
    the ``http`` scheme.
    """
    s = s.strip()
    if not s:
        return False, "Empty proxy string"

    # ------------------------------------------------------------------ #
    # Explicit scheme (http://, https://, socks5://, socks4://)
    # ------------------------------------------------------------------ #
    if "://" in s:
        try:
            parsed = urlparse(s)
        except Exception as exc:
            return False, f"URL parse error: {exc}"

        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https", "socks5", "socks4"):
            return False, f"Unsupported proxy scheme '{scheme}' in: {s!r}"

        host = parsed.hostname
        port = parsed.port
        if not host:
            return False, f"Missing host in: {s!r}"
        if not port:
            return False, f"Missing port in: {s!r}"
        if not (1 <= port <= 65535):
            return False, f"Port {port} out of range in: {s!r}"

        if parsed.username:
            user = parsed.username
            pwd  = parsed.password or ""
            return True, f"{scheme}://{user}:{pwd}@{host}:{port}"
        return True, f"{scheme}://{host}:{port}"

    # ------------------------------------------------------------------ #
    # user:pass@host:port
    # ------------------------------------------------------------------ #
    if "@" in s:
        credentials, hostport = s.rsplit("@", 1)
        if ":" not in hostport:
            return False, f"Missing port in host part of: {s!r}"
        host, port_str = hostport.rsplit(":", 1)
        host = host.strip()
        if not host:
            return False, f"Empty host in: {s!r}"
        try:
            port = int(port_str)
        except ValueError:
            return False, f"Non-numeric port {port_str!r} in: {s!r}"
        if not (1 <= port <= 65535):
            return False, f"Port {port} out of range in: {s!r}"
        if ":" in credentials:
            user, pwd = credentials.split(":", 1)
        else:
            user, pwd = credentials, ""
        return True, f"http://{user}:{pwd}@{host}:{port}"

    # ------------------------------------------------------------------ #
    # host:port  or  host:port:user:pass
    # ------------------------------------------------------------------ #
    parts = s.split(":")
    if len(parts) == 2:
        host, port_str = parts
        host = host.strip()
        if not host:
            return False, f"Empty host in: {s!r}"
        try:
            port = int(port_str)
        except ValueError:
            return False, f"Non-numeric port {port_str!r} in: {s!r}"
        if not (1 <= port <= 65535):
            return False, f"Port {port} out of range in: {s!r}"
        return True, f"http://{host}:{port}"

    if len(parts) == 4:
        host, port_str, user, pwd = parts
        host = host.strip()
        if not host:
            return False, f"Empty host in: {s!r}"
        try:
            port = int(port_str)
        except ValueError:
            return False, f"Non-numeric port {port_str!r} in: {s!r}"
        if not (1 <= port <= 65535):
            return False, f"Port {port} out of range in: {s!r}"
        return True, f"http://{user}:{pwd}@{host}:{port}"

    return False, f"Unrecognised proxy format: {s!r}"


# ---------------------------------------------------------------------------
# 7. parse_proxy_lines(text)
# ---------------------------------------------------------------------------

def parse_proxy_lines(text: str) -> Tuple[List[str], List[str]]:
    """Parse a multi-line block of proxy strings.

    Blank lines and lines beginning with ``#`` are silently skipped.

    Parameters
    ----------
    text:
        Raw text containing one proxy per line.

    Returns
    -------
    ``(valid_list, error_list)`` where *valid_list* contains normalized
    proxy URLs and *error_list* contains human-readable error descriptions
    for every line that failed validation.
    """
    valid:  List[str] = []
    errors: List[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ok, result = normalize_proxy(line)
        if ok:
            valid.append(result)
        else:
            errors.append(f"Line {lineno}: {result}")

    return valid, errors


# ---------------------------------------------------------------------------
# 8. USER_AGENTS
# ---------------------------------------------------------------------------

USER_AGENTS: List[str] = [
    # Windows — Chrome 124
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # macOS Sonoma — Chrome 124
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # Linux x86_64 — Chrome 123
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    # Windows — Chrome 123 (slightly older, common in the wild)
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.6312.86 Safari/537.36"
    ),
    # macOS — Chrome 122
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
]

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pass
