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
# Backend imports  (stdlib; not needed by the helpers above)
# ---------------------------------------------------------------------------

import base64
import random
import select
import socket

# ---------------------------------------------------------------------------
# Backend constants
# ---------------------------------------------------------------------------

_BUFSIZE:       int = 65536   # relay read/write chunk size
_RELAY_TIMEOUT: int = 30      # seconds; applied to select() and socket timeouts

# ---------------------------------------------------------------------------
# _TunnelHandler — per-connection bidirectional relay thread
# ---------------------------------------------------------------------------


class _TunnelHandler(threading.Thread):
    """Relay one browser↔upstream-proxy connection.

    Receives the full HTTP request from the browser, injects the
    ``Proxy-Authorization`` header (if any), forwards everything to the
    upstream proxy, then relays data in both directions until either side
    closes the connection or the idle timeout fires.
    """

    def __init__(
        self,
        client_sock: socket.socket,
        upstream_host: str,
        upstream_port: int,
        auth_header: str,
    ) -> None:
        super().__init__(daemon=True)
        self._client        = client_sock
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._auth          = auth_header   # e.g. "Proxy-Authorization: Basic …\r\n"

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        upstream: Optional[socket.socket] = None
        try:
            # ---- 1. Buffer the full request headers from the browser ----
            data = b""
            self._client.settimeout(_RELAY_TIMEOUT)
            while b"\r\n\r\n" not in data:
                chunk = self._client.recv(_BUFSIZE)
                if not chunk:
                    return
                data += chunk

            # ---- 2. Open connection to the upstream proxy ---------------
            upstream = socket.create_connection(
                (self._upstream_host, self._upstream_port),
                timeout=_RELAY_TIMEOUT,
            )

            # ---- 3. Inject auth header after the first request line -----
            if self._auth:
                eol = data.index(b"\r\n")
                data = data[: eol + 2] + self._auth.encode() + data[eol + 2 :]

            upstream.sendall(data)

            # ---- 4. Bidirectional relay until EOF or timeout ------------
            self._relay(self._client, upstream)

        except Exception:
            pass
        finally:
            for sock in (self._client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Bidirectional relay
    # ------------------------------------------------------------------

    def _relay(self, sock_a: socket.socket, sock_b: socket.socket) -> None:
        """Shuttle bytes between *sock_a* and *sock_b* using ``select``."""
        sock_a.setblocking(False)
        sock_b.setblocking(False)
        sockets = [sock_a, sock_b]

        while True:
            try:
                readable, _, exceptional = select.select(
                    sockets, [], sockets, _RELAY_TIMEOUT
                )
            except Exception:
                return

            if exceptional or not readable:
                return

            for src in readable:
                dst = sock_b if src is sock_a else sock_a
                try:
                    chunk = src.recv(_BUFSIZE)
                    if not chunk:
                        return
                    dst.sendall(chunk)
                except Exception:
                    return


# ---------------------------------------------------------------------------
# ProxyTunnel — local TCP proxy that forwards to an upstream HTTP proxy
# ---------------------------------------------------------------------------


class ProxyTunnel:
    """Bind a free loopback port and forward all connections to an upstream proxy.

    The browser is pointed at ``http://127.0.0.1:<free_port>``; each
    accepted connection is handled by a ``_TunnelHandler`` daemon thread
    which injects ``Proxy-Authorization`` before forwarding to the real
    upstream host.

    Parameters
    ----------
    proxy_url:
        A normalized proxy URL produced by :func:`normalize_proxy`, e.g.
        ``http://user:pass@proxy.example.com:8080`` or
        ``socks5://proxy.example.com:1080``.
    log_cb:
        Optional callable ``(message: str) -> None`` for status messages.
    """

    def __init__(self, proxy_url: str, log_cb=None) -> None:
        self._log_cb = log_cb

        parsed = urlparse(proxy_url)
        self.proto:         str = (parsed.scheme or "http").lower()
        self.upstream_host: str = parsed.hostname or ""
        self.upstream_port: int = parsed.port or 8080
        self._user:         str = parsed.username or ""
        self._pwd:          str = parsed.password or ""

        # Build Proxy-Authorization header once at construction time
        if self._user:
            creds = base64.b64encode(
                f"{self._user}:{self._pwd}".encode()
            ).decode()
            self._auth_header: str = f"Proxy-Authorization: Basic {creds}\r\n"
        else:
            self._auth_header = ""

        self.port: int             = self._find_free_port()
        self._server_sock: Optional[socket.socket] = None
        self._running: bool        = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind the loopback port and begin accepting connections."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("127.0.0.1", self.port))
        self._server_sock.listen(64)
        self._running = True

        t = threading.Thread(target=self._accept_loop, daemon=True, name=f"tunnel-{self.port}")
        t.start()
        self._log(
            f"ProxyTunnel started  127.0.0.1:{self.port} → "
            f"{self.upstream_host}:{self.upstream_port}"
        )

    def stop(self) -> None:
        """Stop accepting new connections and close the server socket."""
        self._running = False
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        self._log(f"ProxyTunnel stopped (port {self.port})")

    # ------------------------------------------------------------------
    # Accept loop
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client_sock, _addr = self._server_sock.accept()
            except Exception:
                break
            _TunnelHandler(
                client_sock,
                self.upstream_host,
                self.upstream_port,
                self._auth_header,
            ).start()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_free_port() -> int:
        """Return an OS-assigned free TCP port on 127.0.0.1."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _log(self, msg: str) -> None:
        if self._log_cb:
            self._log_cb(msg)
        else:
            logger.debug(msg)

    # ------------------------------------------------------------------
    # Property
    # ------------------------------------------------------------------

    @property
    def local_url(self) -> str:
        """The proxy URL to hand to the browser, e.g. ``http://127.0.0.1:54321``."""
        return f"http://127.0.0.1:{self.port}"


# ---------------------------------------------------------------------------
# BrowserSession — one isolated Chrome/Brave process with crash recovery
# ---------------------------------------------------------------------------


class BrowserSession:
    """Manage the full lifecycle of a single browser session.

    Responsibilities
    ----------------
    * Spin up a :class:`ProxyTunnel` when a proxy URL is supplied.
    * Launch the browser in ``--incognito`` mode with a throw-away profile.
    * Monitor the process; restart on crash up to *config[crash_max_retries]*.
    * The tunnel persists across restarts so the upstream proxy connection is
      unaffected by browser crashes.

    Parameters
    ----------
    sid:          Unique session identifier string.
    name:         Human-readable label shown in the UI.
    url:          Initial URL to open (may be empty string).
    proxy:        Normalized proxy URL (``""`` for no proxy).
    browser_path: Absolute path to the Chrome/Brave executable.
    config:       Reference to the application :data:`CONFIG` dict.
    log_cb:       ``(message: str) -> None`` — receives log lines.
    status_cb:    ``(sid: str, status: str) -> None`` — receives status changes.
    """

    def __init__(
        self,
        sid: str,
        name: str,
        url: str,
        proxy: str,
        browser_path: str,
        config: dict,
        log_cb,
        status_cb,
    ) -> None:
        self.sid:           str                          = sid
        self.name:          str                          = name
        self.url:           str                          = url
        self.proxy:         str                          = proxy
        self.browser_path:  str                          = browser_path
        self.config:        dict                         = config
        self._log_cb                                     = log_cb
        self._status_cb                                  = status_cb

        self.profile_dir:   Optional[str]                = None
        self.process:       Optional[subprocess.Popen]   = None
        self.running:       bool                         = False
        self.restart_count: int                          = 0
        self.started_at:    Optional[float]              = None
        self.status:        str                          = "idle"
        self._tunnel:       Optional[ProxyTunnel]        = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Create the proxy tunnel (if needed), launch the browser, and begin monitoring."""
        if self.proxy:
            self._tunnel = ProxyTunnel(self.proxy, log_cb=self._log_cb)
            self._tunnel.start()

        self._launch()
        self.running    = True
        self.started_at = time.time()
        self._set_status("running")

        threading.Thread(
            target=self._monitor, daemon=True, name=f"monitor-{self.sid}"
        ).start()

    def stop(self) -> None:
        """Terminate the browser, stop the tunnel, and clean up the profile."""
        self.running = False
        self._set_status("stopping")

        if self.process is not None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            except Exception as exc:
                self._log(f"[{self.name}] error stopping process: {exc}")

        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None

        self._cleanup_profile()
        self._set_status("stopped")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        """True while the browser process is still running."""
        return self.process is not None and self.process.poll() is None

    @property
    def uptime(self) -> str:
        """Elapsed time since :meth:`start` as ``HH:MM:SS``."""
        if self.started_at is None:
            return "00:00:00"
        elapsed = int(time.time() - self.started_at)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ------------------------------------------------------------------
    # Internal — launch
    # ------------------------------------------------------------------

    def _launch(self) -> None:
        """Create a temp profile and spawn the browser process."""
        self.profile_dir = tempfile.mkdtemp(prefix=f"bg_{self.sid}_")

        # User-agent
        ua: str = ""
        if self.config.get("user_agent_rotate", True):
            ua = random.choice(USER_AGENTS)

        # Proxy server argument
        proxy_arg: str = ""
        if self._tunnel is not None:
            proxy_arg = self._tunnel.local_url
        elif self.proxy:
            proxy_arg = self.proxy

        w = self.config.get("window_width",  1280)
        h = self.config.get("window_height", 800)

        args: List[str] = [
            self.browser_path,
            "--incognito",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-translate",
            "--disable-features=TranslateUI",
            "--disable-infobars",
            "--disable-notifications",
            f"--window-size={w},{h}",
        ]

        if proxy_arg:
            args.append(f"--proxy-server={proxy_arg}")
        if ua:
            args.append(f"--user-agent={ua}")
        if self.url:
            args.append(self.url)

        self.process = subprocess.Popen(
            args,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._log(f"[{self.name}] launched PID {self.process.pid}")

    # ------------------------------------------------------------------
    # Internal — crash monitor
    # ------------------------------------------------------------------

    def _monitor(self) -> None:
        """Poll the browser process; restart on crash; give up after max retries."""
        max_retries  = self.config.get("crash_max_retries",  5)
        retry_delay  = self.config.get("crash_retry_delay",  3)
        do_restart   = self.config.get("crash_restart", True)

        while self.running:
            time.sleep(1)
            if self.process is None:
                continue

            rc = self.process.poll()
            if rc is None:
                continue  # still running

            if not self.running:
                break   # stop() was called while we were sleeping

            if rc == 0:
                self._log(f"[{self.name}] exited cleanly (rc=0)")
                self._set_status("stopped")
                self.running = False
                break

            # --- crash path ---
            self._log(
                f"[{self.name}] crashed (rc={rc})"
                f"  restart {self.restart_count + 1}/{max_retries}"
            )

            if not do_restart or self.restart_count >= max_retries:
                self._log(f"[{self.name}] max retries reached — giving up")
                self._set_status("crashed")
                self.running = False
                break

            self.restart_count += 1
            self._set_status(f"restarting ({self.restart_count}/{max_retries})")
            self._cleanup_profile()
            time.sleep(retry_delay)

            # Tunnel is intentionally kept alive across restarts
            try:
                self._launch()
            except Exception as exc:
                self._log(f"[{self.name}] relaunch failed: {exc}")
                self._set_status("crashed")
                self.running = False
                break

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    def _cleanup_profile(self) -> None:
        if self.profile_dir and os.path.isdir(self.profile_dir):
            try:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            except Exception as exc:
                self._log(f"[{self.name}] profile cleanup error: {exc}")
        self.profile_dir = None

    def _log(self, msg: str) -> None:
        if self._log_cb:
            self._log_cb(msg)
        else:
            logger.info(msg)

    def _set_status(self, status: str) -> None:
        self.status = status
        if self._status_cb:
            self._status_cb(self.sid, status)
        logger.debug("[%s] status → %s", self.name, status)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pass
