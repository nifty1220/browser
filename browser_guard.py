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
# UI imports  (stdlib; not needed by backend classes above)
# ---------------------------------------------------------------------------

import queue

# ---------------------------------------------------------------------------
# UI constants
# ---------------------------------------------------------------------------

_CONFIG_DIR:  pathlib.Path = pathlib.Path.home() / ".browserguard"
_CONFIG_FILE: pathlib.Path = _CONFIG_DIR / "config.json"
_TOOLBAR_H:   int          = 48
_SIDEBAR_W:   int          = 260

# ---------------------------------------------------------------------------
# BrowserGuardApp
# ---------------------------------------------------------------------------


class BrowserGuardApp:
    """Root application controller and main window for BrowserGuard."""

    # ======================================================================
    # Construction
    # ======================================================================

    def __init__(self, root: tk.Tk) -> None:
        self.root = root

        # ---- mutable state -----------------------------------------------
        self.sessions:      Dict[str, BrowserSession] = {}
        self.session_cards: Dict[str, tk.Frame]        = {}
        self.proxy_list:    List[str]                  = []
        self.website_list:  List[str]                  = []
        self.proxy_index:   int                        = 0
        self.log_queue:     queue.Queue                = queue.Queue()
        self._sid_counter:  int                        = 0

        # ---- browser discovery state -------------------------------------
        self._selected_browser: str          = "auto"
        self._brave_path:       Optional[str] = None
        self._chrome_path:      Optional[str] = None
        self._browser_path:     Optional[str] = None

        # ---- load config (also populates proxy_list / website_list) ------
        self._load_config()
        self._dark_mode: bool = bool(CONFIG.get("dark_mode", False))

        # ---- theming registry: widget → {tk_option: theme_color_key} ----
        self._themed_widgets: Dict[object, Dict[str, str]] = {}

        # ---- window & fonts ----------------------------------------------
        self._setup_window()
        _resolve_fonts()

        # ---- build UI ----------------------------------------------------
        self._build_layout()

        # ---- window close → save & stop all ------------------------------
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- browser detection in background -----------------------------
        threading.Thread(
            target=self._detect_browsers, daemon=True, name="browser-detect"
        ).start()

        # ---- periodic log-queue drain ------------------------------------
        self.root.after(100, self._drain_log)

    # ======================================================================
    # Config  (_load / _save)
    # ======================================================================

    def _load_config(self) -> None:
        """Read ~/.browserguard/config.json and merge into CONFIG."""
        try:
            if _CONFIG_FILE.exists():
                data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
                CONFIG.update(data)
        except Exception as exc:
            logger.warning("Could not load config: %s", exc)

        self.proxy_list        = list(CONFIG.get("proxy_list",   []))
        self.website_list      = list(CONFIG.get("website_list", []))
        self._selected_browser = str(CONFIG.get("selected_browser", "auto"))

    def _save_config(self) -> None:
        """Persist current state back to ~/.browserguard/config.json."""
        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG["proxy_list"]       = self.proxy_list
            CONFIG["website_list"]     = self.website_list
            CONFIG["selected_browser"] = self._selected_browser
            CONFIG["dark_mode"]        = self._dark_mode
            _CONFIG_FILE.write_text(
                json.dumps(CONFIG, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Could not save config: %s", exc)

    # ======================================================================
    # Window setup
    # ======================================================================

    def _setup_window(self) -> None:
        """Configure the root Tk window — title, size, minimum dimensions."""
        self.root.title("BrowserGuard")
        self.root.minsize(900, 580)

        w  = int(CONFIG.get("window_width",  1280))
        h  = int(CONFIG.get("window_height", 800))
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        if w >= sw:
            # Maximise; fall back to -zoomed on platforms without "zoomed" state
            try:
                self.root.state("zoomed")
            except tk.TclError:
                try:
                    self.root.attributes("-zoomed", True)
                except tk.TclError:
                    pass
        else:
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.root.bind("<Configure>", self._on_resize)

    def _on_resize(self, event: tk.Event) -> None:
        """Keep CONFIG in sync with the live window dimensions."""
        if event.widget is self.root:
            CONFIG["window_width"]  = self.root.winfo_width()
            CONFIG["window_height"] = self.root.winfo_height()

    def _on_close(self) -> None:
        """Save geometry + config then destroy the window."""
        CONFIG["window_width"]  = self.root.winfo_width()
        CONFIG["window_height"] = self.root.winfo_height()
        self._stop_all()
        self._save_config()
        self.root.destroy()

    # ======================================================================
    # Theme
    # ======================================================================

    def _get_colors(self) -> Dict[str, str]:
        """Return the active colour palette dict."""
        return THEME["dark"] if self._dark_mode else THEME["light"]

    def _tw(self, widget, **tags: str):
        """Register *widget* in the theme registry and return it.

        *tags* maps tkinter option names to THEME colour keys, e.g.
        ``_tw(lbl, background="card", foreground="text")``.
        """
        self._themed_widgets[widget] = tags
        return widget

    def _apply_theme(self) -> None:
        """Repaint every registered widget using the current colour palette.

        Walks the full widget tree so that widgets added after the initial
        build are also updated as long as they were registered with :meth:`_tw`.
        """
        c = self._get_colors()

        def _walk(w: tk.BaseWidget) -> None:
            tags = self._themed_widgets.get(w, {})
            kw   = {opt: c[key] for opt, key in tags.items() if key in c}
            if kw:
                try:
                    w.configure(**kw)
                except tk.TclError:
                    pass
            for child in w.winfo_children():
                _walk(child)

        _walk(self.root)

        # Segmented control colours are managed separately
        self._refresh_segmented()

        # Theme-toggle icon
        if hasattr(self, "_theme_btn"):
            self._theme_btn.configure(
                text="☀" if self._dark_mode else "☾",
                foreground=self._get_colors()["text2"],
            )

    def _toggle_dark_mode(self) -> None:
        """Flip between dark and light mode, persist, and repaint."""
        self._dark_mode = not self._dark_mode
        self._save_config()
        self._apply_theme()

    # ======================================================================
    # Layout  (three-zone structure)
    # ======================================================================

    def _build_layout(self) -> None:
        """Build toolbar + sidebar/content split."""
        self._tw(self.root, background="bg")

        # ── Zone 1: toolbar ───────────────────────────────────────────────
        self._build_toolbar()

        # ── 1-px horizontal separator ────────────────────────────────────
        self._tw(tk.Frame(self.root, height=1), background="separator").pack(fill=tk.X)

        # ── Zone 2 & 3: sidebar + content in a shared row ─────────────────
        main = self._tw(tk.Frame(self.root), background="bg")
        main.pack(fill=tk.BOTH, expand=True)

        self._build_sidebar(main)

        # 1-px vertical divider between sidebar and content
        self._tw(tk.Frame(main, width=1), background="separator").pack(
            side=tk.LEFT, fill=tk.Y
        )

        self._build_content(main)

    # ======================================================================
    # Toolbar
    # ======================================================================

    def _build_toolbar(self) -> None:
        """48-px top bar: logo · segmented control · spacer · toggle · badge · new-btn."""
        bar = self._tw(tk.Frame(self.root, height=_TOOLBAR_H), background="toolbar")
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        # ── Logo ──────────────────────────────────────────────────────────
        self._tw(
            tk.Label(bar, text="BrowserGuard", font=f(15, "bold"), padx=16),
            background="toolbar", foreground="text",
        ).pack(side=tk.LEFT)

        # ── Browser segmented control ────────────────────────────────────
        # Outer frame acts as the 1-px border via its background colour.
        seg_outer = self._tw(
            tk.Frame(bar, bd=1, relief=tk.FLAT),
            background="separator",
        )
        seg_outer.pack(side=tk.LEFT, padx=(0, 8), pady=10)

        self._seg_brave = tk.Label(
            seg_outer, text="Brave", font=f(12), padx=12, pady=2, cursor="hand2"
        )
        self._seg_brave.pack(side=tk.LEFT)
        self._seg_brave.bind("<Button-1>", lambda _e: self._switch_browser("brave"))

        # 1-px divider between the two segments
        self._tw(
            tk.Frame(seg_outer, width=1), background="separator"
        ).pack(side=tk.LEFT, fill=tk.Y)

        self._seg_chrome = tk.Label(
            seg_outer, text="Chrome", font=f(12), padx=12, pady=2, cursor="hand2"
        )
        self._seg_chrome.pack(side=tk.LEFT)
        self._seg_chrome.bind("<Button-1>", lambda _e: self._switch_browser("chrome"))

        # Register with empty tags — _refresh_segmented owns their colours
        self._themed_widgets[self._seg_brave]  = {}
        self._themed_widgets[self._seg_chrome] = {}

        # ── Spacer ────────────────────────────────────────────────────────
        self._tw(
            tk.Frame(bar), background="toolbar"
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── Dark / light toggle ───────────────────────────────────────────
        self._theme_btn = self._tw(
            tk.Label(
                bar,
                text="☾" if not self._dark_mode else "☀",
                font=f(16), padx=10, cursor="hand2",
            ),
            background="toolbar", foreground="text2",
        )
        self._theme_btn.pack(side=tk.LEFT, pady=6)
        self._theme_btn.bind("<Button-1>", lambda _e: self._toggle_dark_mode())

        # ── Session count badge ───────────────────────────────────────────
        self._badge_var = tk.StringVar(value="0 active")
        self._tw(
            tk.Label(bar, textvariable=self._badge_var, font=f(11), padx=10),
            background="toolbar", foreground="text2",
        ).pack(side=tk.LEFT, pady=6)

        # ── ＋ New Session button ─────────────────────────────────────────
        new_btn = self._tw(
            tk.Label(
                bar, text="＋ New Session", font=f(12, "bold"),
                padx=14, pady=4, cursor="hand2",
            ),
            background="accent", foreground="card",
        )
        new_btn.pack(side=tk.LEFT, padx=(4, 12), pady=8)
        new_btn.bind("<Button-1>", lambda _e: self._new_session())

        # Paint the segmented control for the first time
        self._refresh_segmented()

    # ======================================================================
    # Sidebar
    # ======================================================================

    def _build_sidebar(self, parent: tk.Frame) -> None:
        """260-px left panel: search input · scrollable cards · Stop All."""
        sidebar = self._tw(tk.Frame(parent, width=_SIDEBAR_W), background="bg")
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        # ── Search input ──────────────────────────────────────────────────
        s_wrap = self._tw(tk.Frame(sidebar, pady=8), background="bg")
        s_wrap.pack(fill=tk.X, padx=12)

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter_cards())

        self._search_entry = self._tw(
            tk.Entry(
                s_wrap,
                textvariable=self._search_var,
                font=f(12), relief=tk.FLAT, bd=0,
            ),
            background="card", foreground="text",
            insertbackground="text",
            highlightbackground="separator", highlightcolor="accent",
        )
        self._search_entry.pack(fill=tk.X, ipady=6, padx=2)
        self._set_placeholder(self._search_entry, "Search sessions…")

        self._tw(
            tk.Frame(sidebar, height=1), background="separator"
        ).pack(fill=tk.X, padx=12)

        # ── Scrollable session-card area ──────────────────────────────────
        canvas_wrap = self._tw(tk.Frame(sidebar), background="bg")
        canvas_wrap.pack(fill=tk.BOTH, expand=True)

        self._cards_canvas = self._tw(
            tk.Canvas(canvas_wrap, highlightthickness=0, bd=0),
            background="bg",
        )
        self._cards_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        cards_sb = ttk.Scrollbar(
            canvas_wrap, orient=tk.VERTICAL, command=self._cards_canvas.yview
        )
        cards_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cards_canvas.configure(yscrollcommand=cards_sb.set)

        self._cards_inner = self._tw(
            tk.Frame(self._cards_canvas), background="bg"
        )
        self._cards_win = self._cards_canvas.create_window(
            (0, 0), window=self._cards_inner, anchor="nw"
        )

        self._cards_inner.bind("<Configure>", self._on_cards_configure)
        self._cards_canvas.bind("<Configure>", self._on_canvas_configure)

        # ── Stop All (destructive, pinned to bottom) ──────────────────────
        stop_all = self._tw(
            tk.Label(
                sidebar, text="⏹  Stop All", font=f(12, "bold"),
                padx=12, pady=8, cursor="hand2",
            ),
            background="danger", foreground="card",
        )
        stop_all.pack(fill=tk.X, padx=12, pady=(4, 12))
        stop_all.bind("<Button-1>", lambda _e: self._stop_all())

    def _on_cards_configure(self, _event: object) -> None:
        if not hasattr(self, "_cards_canvas"):
            return
        self._cards_canvas.configure(
            scrollregion=self._cards_canvas.bbox("all")
        )

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._cards_canvas.itemconfigure(self._cards_win, width=event.width)

    # ======================================================================
    # Content area  (placeholder — pages added later)
    # ======================================================================

    def _build_content(self, parent: tk.Frame) -> None:
        """Right-side content area with tab bar + four pages."""
        self._content_frame = self._tw(tk.Frame(parent), background="bg")
        self._content_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── Tab bar ────────────────────────────────────────────────────────
        tab_bar = self._tw(tk.Frame(self._content_frame, height=40), background="card")
        tab_bar.pack(fill=tk.X)
        tab_bar.pack_propagate(False)

        self._tab_labels: Dict[str, tk.Label] = {}
        self._tab_underlines: Dict[str, tk.Frame] = {}
        self._tab_names = ["launcher", "websites", "proxies", "log"]
        _tab_texts = {
            "launcher": "🚀  Launcher",
            "websites": "🌐  Websites",
            "proxies":  "🔀  Proxies",
            "log":      "📄  Log",
        }
        for key, text in _tab_texts.items():
            wrap = self._tw(tk.Frame(tab_bar), background="card")
            wrap.pack(side=tk.LEFT)
            lbl = self._tw(
                tk.Label(wrap, text=text, font=f(12), padx=16, pady=6, cursor="hand2"),
                background="card", foreground="text2",
            )
            lbl.pack()
            ul = self._tw(tk.Frame(wrap, height=2), background="card")
            ul.pack(fill=tk.X)
            self._tab_labels[key] = lbl
            self._tab_underlines[key] = ul
            lbl.bind("<Button-1>", lambda _e, k=key: self._switch_tab(k))

        # ── Page container ─────────────────────────────────────────────────
        self._pages_frame = self._tw(tk.Frame(self._content_frame), background="bg")
        self._pages_frame.pack(fill=tk.BOTH, expand=True)

        self._page_frames: Dict[str, tk.Frame] = {}
        for key in self._tab_names:
            page = self._tw(tk.Frame(self._pages_frame), background="bg")
            self._page_frames[key] = page
            getattr(self, f"_build_{key}_page")(page)

        # ── Start uptime ticker ────────────────────────────────────────────
        self.root.after(1000, self._tick_uptimes)

        # ── Show first tab ────────────────────────────────────────────────
        self._active_tab: str = ""
        self._switch_tab("launcher")

    # ======================================================================
    # Browser detection & segmented control
    # ======================================================================

    def _detect_browsers(self) -> None:
        """Run in a background thread; update paths then refresh the UI."""
        brave  = find_brave()
        chrome = find_chrome()
        self._brave_path  = brave
        self._chrome_path = chrome

        # Pick an initial browser path consistent with the saved preference
        sel = self._selected_browser
        if sel == "brave" and brave:
            self._browser_path = brave
        elif sel == "chrome" and chrome:
            self._browser_path = chrome
        elif brave:
            self._browser_path     = brave
            self._selected_browser = "brave"
        elif chrome:
            self._browser_path     = chrome
            self._selected_browser = "chrome"

        logger.debug("Browsers — Brave: %s  Chrome: %s", brave, chrome)
        # Schedule GUI update on the main thread
        self.root.after(0, self._refresh_segmented)

    def _switch_browser(self, key: str) -> None:
        """Select *key* ('brave'|'chrome'), update path, persist, repaint."""
        if key == "brave":
            if not self._brave_path:
                return
            self._selected_browser = "brave"
            self._browser_path     = self._brave_path
        elif key == "chrome":
            if not self._chrome_path:
                return
            self._selected_browser = "chrome"
            self._browser_path     = self._chrome_path
        else:
            return
        CONFIG["selected_browser"] = self._selected_browser
        self._save_config()
        self._refresh_segmented()

    def _refresh_segmented(self) -> None:
        """Repaint the Brave/Chrome toggle to reflect the active selection."""
        if not (hasattr(self, "_seg_brave") and hasattr(self, "_seg_chrome")):
            return
        c = self._get_colors()
        for key, widget in (("brave", self._seg_brave), ("chrome", self._seg_chrome)):
            active = (self._selected_browser == key)
            widget.configure(
                background=c["card"]    if active else c["surface2"],
                foreground=c["text"]    if active else c["text2"],
                font=f(12, "bold" if active else "normal"),
            )

    # ======================================================================
    # Search / card filter
    # ======================================================================

    def _filter_cards(self) -> None:
        """Show/hide session cards based on the search-entry contents."""
        raw   = self._search_var.get()
        query = "" if raw == "Search sessions…" else raw.lower().strip()

        for sid, card in self.session_cards.items():
            session = self.sessions.get(sid)
            if session is None:
                card.pack_forget()
                continue
            if not query or query in session.name.lower() or query in session.url.lower():
                card.pack(fill=tk.X, padx=8, pady=(0, 4))
            else:
                card.pack_forget()

        self._on_cards_configure(None)

    # ======================================================================
    # Session management stubs
    # ======================================================================

    def _new_session(self) -> None:
        """Open the new-session dialog (implemented when content pages are added)."""
        logger.debug("New session requested")

    def _stop_all(self) -> None:
        """Terminate every running session."""
        for session in list(self.sessions.values()):
            try:
                session.stop()
            except Exception as exc:
                logger.warning("stop_all error [%s]: %s", session.sid, exc)
        self._update_badge()
        if hasattr(self, "_stat_active_var"):
            self._update_stats()
        if hasattr(self, "_log_text"):
            self.log("All sessions stopped.", "warning")

    def _update_badge(self) -> None:
        """Refresh the active-session count badge in the toolbar."""
        count = sum(1 for s in self.sessions.values() if s.running)
        self._badge_var.set(f"{count} active")

    # ======================================================================
    # Tab switching
    # ======================================================================

    def _switch_tab(self, key: str) -> None:
        """Show the *key* page and update the tab bar appearance."""
        c = self._get_colors()
        for k, lbl in self._tab_labels.items():
            active = (k == key)
            lbl.configure(
                foreground=c["accent"] if active else c["text2"],
                font=f(12, "bold" if active else "normal"),
            )
            self._tab_underlines[k].configure(
                background=c["accent"] if active else c["card"],
            )
        if hasattr(self, "_active_tab") and self._active_tab in self._page_frames:
            self._page_frames[self._active_tab].pack_forget()
        self._active_tab = key
        self._page_frames[key].pack(fill=tk.BOTH, expand=True)

    # ======================================================================
    # PAGE 1 — Launcher
    # ======================================================================

    def _build_launcher_page(self, parent: tk.Frame) -> None:
        """Build the Launcher page."""
        pad = {"padx": 16, "pady": 8}

        scroll_canvas = self._tw(
            tk.Canvas(parent, highlightthickness=0, bd=0), background="bg"
        )
        scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=scroll_canvas.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_canvas.configure(yscrollcommand=vsb.set)

        inner = self._tw(tk.Frame(scroll_canvas), background="bg")
        win = scroll_canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_e):
            scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))

        def _on_canvas_configure(e):
            scroll_canvas.itemconfigure(win, width=e.width)

        inner.bind("<Configure>", _on_inner_configure)
        scroll_canvas.bind("<Configure>", _on_canvas_configure)

        # ── Group: Session ─────────────────────────────────────────────────
        sess_card = self._make_card(inner, "Session")
        sess_card.pack(fill=tk.X, **pad)

        row = self._tw(tk.Frame(sess_card), background="card")
        row.pack(fill=tk.X, padx=12, pady=(0, 10))

        self._tw(
            tk.Label(row, text="Name", font=f(11), width=6, anchor="w"),
            background="card", foreground="text2",
        ).pack(side=tk.LEFT)
        self._launcher_name = tk.StringVar()
        self._tw(
            tk.Entry(row, textvariable=self._launcher_name, font=f(12),
                     relief=tk.FLAT, bd=0),
            background="surface2", foreground="text", insertbackground="text",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(4, 12))

        self._tw(
            tk.Label(row, text="URL", font=f(11), anchor="w"),
            background="card", foreground="text2",
        ).pack(side=tk.LEFT)
        self._launcher_url = tk.StringVar()
        self._tw(
            tk.Entry(row, textvariable=self._launcher_url, font=f(12),
                     relief=tk.FLAT, bd=0),
            background="surface2", foreground="text", insertbackground="text",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(4, 12))

        self._tw(
            tk.Label(row, text="Saved", font=f(11), anchor="w"),
            background="card", foreground="text2",
        ).pack(side=tk.LEFT)
        self._site_combo = ttk.Combobox(row, font=f(12), width=22, state="readonly")
        self._site_combo.pack(side=tk.LEFT, padx=(4, 0))
        self._site_combo.bind("<<ComboboxSelected>>", self._on_site_selected)
        self._refresh_site_combo()

        # ── Group: Proxy ───────────────────────────────────────────────────
        proxy_card = self._make_card(inner, "Proxy")
        proxy_card.pack(fill=tk.X, **pad)

        p_row = self._tw(tk.Frame(proxy_card), background="card")
        p_row.pack(fill=tk.X, padx=12, pady=(0, 10))

        self._proxy_mode_var = tk.StringVar(value=CONFIG.get("proxy_mode", "none"))
        for mode, label in [("none", "None"), ("single", "Single"),
                             ("rotate", "Rotate"), ("random", "Random")]:
            rb = tk.Radiobutton(
                p_row, text=label, variable=self._proxy_mode_var, value=mode,
                font=f(12), cursor="hand2", relief=tk.FLAT, bd=0,
                activebackground=self._get_colors()["card"],
                command=self._on_proxy_mode_change,
            )
            self._tw(rb, background="card", foreground="text", selectcolor="card")
            rb.pack(side=tk.LEFT, padx=(0, 8))

        self._proxy_preview_var = tk.StringVar(value="—")
        self._tw(
            tk.Label(p_row, textvariable=self._proxy_preview_var, font=f(11)),
            background="card", foreground="text3",
        ).pack(side=tk.LEFT, padx=(16, 0))
        self._update_proxy_preview()

        # ── Group: Options ─────────────────────────────────────────────────
        opts_card = self._make_card(inner, "Options")
        opts_card.pack(fill=tk.X, **pad)

        o_row = self._tw(tk.Frame(opts_card), background="card")
        o_row.pack(fill=tk.X, padx=12, pady=(0, 10))

        self._opt_crash_restart = tk.BooleanVar(value=bool(CONFIG.get("crash_restart", True)))
        cb1 = tk.Checkbutton(
            o_row, text="Auto-restart on crash",
            variable=self._opt_crash_restart, font=f(12), cursor="hand2",
            relief=tk.FLAT, bd=0,
            activebackground=self._get_colors()["card"],
            command=lambda: CONFIG.update({"crash_restart": self._opt_crash_restart.get()}),
        )
        self._tw(cb1, background="card", foreground="text", selectcolor="card")
        cb1.pack(side=tk.LEFT, padx=(0, 16))

        self._opt_ua_rotate = tk.BooleanVar(value=bool(CONFIG.get("user_agent_rotate", True)))
        cb2 = tk.Checkbutton(
            o_row, text="Rotate user-agent",
            variable=self._opt_ua_rotate, font=f(12), cursor="hand2",
            relief=tk.FLAT, bd=0,
            activebackground=self._get_colors()["card"],
            command=lambda: CONFIG.update({"user_agent_rotate": self._opt_ua_rotate.get()}),
        )
        self._tw(cb2, background="card", foreground="text", selectcolor="card")
        cb2.pack(side=tk.LEFT)

        # ── Group: Multi-Launch ────────────────────────────────────────────
        multi_card = self._make_card(inner, "Multi-Launch")
        multi_card.pack(fill=tk.X, **pad)

        m_row = self._tw(tk.Frame(multi_card), background="card")
        m_row.pack(fill=tk.X, padx=12, pady=(0, 10))

        self._tw(
            tk.Label(m_row, text="Instances:", font=f(12)),
            background="card", foreground="text",
        ).pack(side=tk.LEFT)

        self._multi_count = tk.IntVar(value=1)
        sb = tk.Spinbox(
            m_row, from_=1, to=50, textvariable=self._multi_count,
            width=4, font=f(12), relief=tk.FLAT, bd=1,
        )
        self._tw(sb, background="surface2", foreground="text")
        sb.pack(side=tk.LEFT, padx=8)

        self._tw(
            tk.Label(m_row, text="Each instance gets its own profile & proxy slot.",
                     font=f(10)),
            background="card", foreground="text3",
        ).pack(side=tk.LEFT, padx=(8, 0))

        # ── Start button ───────────────────────────────────────────────────
        btn_wrap = self._tw(tk.Frame(inner), background="bg")
        btn_wrap.pack(fill=tk.X, padx=16, pady=(4, 8))

        start_btn = self._tw(
            tk.Label(
                btn_wrap, text="▶  Start Session", font=f(13, "bold"),
                padx=20, pady=10, cursor="hand2",
            ),
            background="accent", foreground="card",
        )
        start_btn.pack(side=tk.LEFT)
        start_btn.bind("<Button-1>", lambda _e: self._launch_multi())

        # ── Stats row ──────────────────────────────────────────────────────
        stats_row = self._tw(tk.Frame(inner), background="bg")
        stats_row.pack(fill=tk.X, padx=16, pady=(0, 16))

        self._stat_active_var = tk.StringVar(value="0")
        self._stat_total_var  = tk.StringVar(value="0")
        self._stat_proxy_var  = tk.StringVar(value="0")

        for var, label in [
            (self._stat_active_var, "Active"),
            (self._stat_total_var,  "Total"),
            (self._stat_proxy_var,  "Proxies"),
        ]:
            box = self._tw(tk.Frame(stats_row, padx=16, pady=8), background="card")
            box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
            self._tw(
                tk.Label(box, textvariable=var, font=f(22, "bold")),
                background="card", foreground="accent",
            ).pack()
            self._tw(
                tk.Label(box, text=label, font=f(11)),
                background="card", foreground="text2",
            ).pack()

        self._update_stats()

    def _make_card(self, parent: tk.Widget, title: str) -> tk.Frame:
        """Return a themed card frame with a title label."""
        card = self._tw(tk.Frame(parent, padx=0, pady=0), background="card")
        self._tw(
            tk.Label(card, text=title.upper(), font=f(9, "bold"),
                     anchor="w", padx=12, pady=6),
            background="card", foreground="text3",
        ).pack(fill=tk.X)
        self._tw(tk.Frame(card, height=1), background="separator").pack(fill=tk.X)
        return card

    def _on_site_selected(self, _event: object) -> None:
        """Populate Launcher Name/URL from saved-site combo selection."""
        sel = self._site_combo.get()
        for site in self.website_list:
            parts = site.split("|", 2)
            name = parts[0] if len(parts) > 0 else ""
            url  = parts[1] if len(parts) > 1 else ""
            if name == sel:
                self._launcher_name.set(name)
                self._launcher_url.set(url)
                break

    def _refresh_site_combo(self) -> None:
        if not hasattr(self, "_site_combo"):
            return
        names = []
        for site in self.website_list:
            parts = site.split("|", 2)
            if parts:
                names.append(parts[0])
        self._site_combo["values"] = names

    def _on_proxy_mode_change(self) -> None:
        CONFIG["proxy_mode"] = self._proxy_mode_var.get()
        self._update_proxy_preview()

    def _update_proxy_preview(self) -> None:
        if not hasattr(self, "_proxy_preview_var"):
            return
        mode = self._proxy_mode_var.get() if hasattr(self, "_proxy_mode_var") else "none"
        if mode == "none" or not self.proxy_list:
            self._proxy_preview_var.set("—")
        elif mode == "single":
            self._proxy_preview_var.set(
                self.proxy_list[self.proxy_index % len(self.proxy_list)]
            )
        elif mode == "rotate":
            self._proxy_preview_var.set(
                f"Next: {self.proxy_list[self.proxy_index % len(self.proxy_list)]}"
            )
        elif mode == "random":
            self._proxy_preview_var.set("(random)")

    def _pick_proxy(self) -> str:
        """Return the proxy to use for the next session per current mode."""
        mode = CONFIG.get("proxy_mode", "none")
        if mode == "none" or not self.proxy_list:
            return ""
        if mode == "single":
            return self.proxy_list[self.proxy_index % len(self.proxy_list)]
        if mode == "rotate":
            proxy = self.proxy_list[self.proxy_index % len(self.proxy_list)]
            self.proxy_index = (self.proxy_index + 1) % len(self.proxy_list)
            self._update_proxy_preview()
            return proxy
        if mode == "random":
            return random.choice(self.proxy_list)
        return ""

    def _launch_multi(self) -> None:
        """Launch N sessions as specified by the Multi-Launch spinbox."""
        count = max(1, min(50, self._multi_count.get()))
        for _ in range(count):
            self.launch_session()

    def launch_session(self) -> None:
        """Create and start a new BrowserSession from Launcher inputs."""
        if not self._browser_path:
            self.log("No browser found — install Brave or Chrome.", "error")
            return
        self._sid_counter += 1
        sid   = str(uuid.uuid4())
        name  = self._launcher_name.get().strip() or f"Session {self._sid_counter}"
        url   = self._launcher_url.get().strip()
        proxy = self._pick_proxy()

        session = BrowserSession(
            sid=sid,
            name=name,
            url=url,
            proxy=proxy,
            browser_path=self._browser_path,
            config=CONFIG,
            log_cb=lambda msg: self.log_queue.put(msg),
            status_cb=self._on_session_status,
        )
        self.sessions[sid] = session
        self._add_session_card(session)

        try:
            session.start()
        except Exception as exc:
            self.log(f"Failed to start '{name}': {exc}", "error")
            return

        self.log(
            f"Started session '{name}'" + (f" via {proxy}" if proxy else ""),
            "success",
        )
        self._update_badge()
        self._update_stats()

    def _on_session_status(self, sid: str, status: str) -> None:
        """Called from background threads on session status changes."""
        self.root.after(0, lambda: self._handle_status_update(sid, status))

    def _handle_status_update(self, sid: str, status: str) -> None:
        self._update_badge()
        self._update_stats()
        session = self.sessions.get(sid)
        if session:
            self.log(f"[{session.name}] status → {status}")
            card = self.session_cards.get(sid)
            if card and hasattr(card, "_status_lbl"):
                try:
                    c = self._get_colors()
                    color = {
                        "running":  c["success"],
                        "crashed":  c["danger"],
                        "stopped":  c["text2"],
                        "stopping": c["warning"],
                    }.get(status, c["text2"])
                    card._status_lbl.configure(text=status, foreground=color)
                except Exception:
                    pass

    def _add_session_card(self, session) -> None:
        """Create a sidebar card widget for *session*."""
        c = self._get_colors()
        card = self._tw(tk.Frame(self._cards_inner, cursor="hand2"), background="card")
        card.pack(fill=tk.X, padx=8, pady=(0, 4))
        self.session_cards[session.sid] = card

        top = self._tw(tk.Frame(card), background="card")
        top.pack(fill=tk.X, padx=8, pady=(6, 2))

        self._tw(
            tk.Label(top, text=session.name, font=f(12, "bold"), anchor="w"),
            background="card", foreground="text",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        stop_btn = self._tw(
            tk.Label(top, text="✕", font=f(11), cursor="hand2", padx=4),
            background="card", foreground="danger",
        )
        stop_btn.pack(side=tk.RIGHT)
        stop_btn.bind("<Button-1>", lambda _e, s=session: self._stop_session(s))

        bottom = self._tw(tk.Frame(card), background="card")
        bottom.pack(fill=tk.X, padx=8, pady=(0, 6))

        status_lbl = self._tw(
            tk.Label(bottom, text="running", font=f(10), anchor="w"),
            background="card", foreground=c["success"],
        )
        status_lbl.pack(side=tk.LEFT)
        card._status_lbl = status_lbl

        uptime_lbl = self._tw(
            tk.Label(bottom, text="00:00:00", font=fm(10), anchor="e"),
            background="card", foreground="text3",
        )
        uptime_lbl.pack(side=tk.RIGHT)
        card._uptime_lbl = uptime_lbl

        if session.proxy:
            self._tw(
                tk.Label(card, text=session.proxy, font=f(9), anchor="w"),
                background="card", foreground="text3",
            ).pack(fill=tk.X, padx=8, pady=(0, 4))

    def _stop_session(self, session) -> None:
        """Stop a single session and remove its card."""
        try:
            session.stop()
        except Exception as exc:
            logger.warning("stop session error: %s", exc)
        self.log(f"Stopped '{session.name}'.", "warning")
        card = self.session_cards.pop(session.sid, None)
        if card:
            card.destroy()
        self.sessions.pop(session.sid, None)
        self._update_badge()
        self._update_stats()

    def _update_stats(self) -> None:
        if not hasattr(self, "_stat_active_var"):
            return
        active = sum(1 for s in self.sessions.values() if s.running)
        self._stat_active_var.set(str(active))
        self._stat_total_var.set(str(len(self.sessions)))
        self._stat_proxy_var.set(str(len(self.proxy_list)))

    def _tick_uptimes(self) -> None:
        """Update uptime labels on session cards every second."""
        for sid, card in list(self.session_cards.items()):
            session = self.sessions.get(sid)
            if session and hasattr(card, "_uptime_lbl"):
                try:
                    card._uptime_lbl.configure(text=session.uptime)
                except Exception:
                    pass
        self.root.after(1000, self._tick_uptimes)

    # ======================================================================
    # PAGE 2 — Websites
    # ======================================================================

    def _build_websites_page(self, parent: tk.Frame) -> None:
        """Build the Websites page."""

        # ── Add card ───────────────────────────────────────────────────────
        add_card = self._tw(tk.Frame(parent), background="card")
        add_card.pack(fill=tk.X, padx=16, pady=(12, 4))

        row = self._tw(tk.Frame(add_card), background="card")
        row.pack(fill=tk.X, padx=12, pady=8)

        self._tw(
            tk.Label(row, text="Name", font=f(11), anchor="w"),
            background="card", foreground="text2",
        ).pack(side=tk.LEFT)
        self._ws_name = tk.StringVar()
        self._tw(
            tk.Entry(row, textvariable=self._ws_name, font=f(12), relief=tk.FLAT, bd=0),
            background="surface2", foreground="text", insertbackground="text",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(4, 12))

        self._tw(
            tk.Label(row, text="URL", font=f(11), anchor="w"),
            background="card", foreground="text2",
        ).pack(side=tk.LEFT)
        self._ws_url = tk.StringVar()
        self._tw(
            tk.Entry(row, textvariable=self._ws_url, font=f(12), relief=tk.FLAT, bd=0),
            background="surface2", foreground="text", insertbackground="text",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(4, 12))

        add_btn = self._tw(
            tk.Label(row, text="Add", font=f(12, "bold"),
                     padx=12, pady=4, cursor="hand2"),
            background="accent", foreground="card",
        )
        add_btn.pack(side=tk.LEFT)
        add_btn.bind("<Button-1>", lambda _e: self._ws_add())

        # ── Treeview ───────────────────────────────────────────────────────
        tree_frame = self._tw(tk.Frame(parent), background="bg")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        cols = ("Name", "URL", "Opens")
        self._ws_tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", selectmode="browse"
        )
        for col in cols:
            self._ws_tree.heading(col, text=col)
        self._ws_tree.column("Name",  width=180, stretch=False)
        self._ws_tree.column("URL",   width=320, stretch=True)
        self._ws_tree.column("Opens", width=60,  stretch=False)
        self._ws_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ws_sb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._ws_tree.yview)
        ws_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._ws_tree.configure(yscrollcommand=ws_sb.set)

        c = self._get_colors()
        self._ws_tree.tag_configure("odd",  background=c["card"])
        self._ws_tree.tag_configure("even", background=c["surface2"])
        self._ws_tree.bind("<Double-1>", self._ws_on_double_click)

        # ── Footer ─────────────────────────────────────────────────────────
        footer = self._tw(tk.Frame(parent), background="bg")
        footer.pack(fill=tk.X, padx=16, pady=(4, 12))

        del_btn = self._tw(
            tk.Label(footer, text="Delete Selected", font=f(12, "bold"),
                     padx=12, pady=6, cursor="hand2"),
            background="danger", foreground="card",
        )
        del_btn.pack(side=tk.LEFT)
        del_btn.bind("<Button-1>", lambda _e: self._ws_delete())

        self._tw(
            tk.Label(footer,
                     text="Double-click a row to load it in the Launcher.",
                     font=f(10)),
            background="bg", foreground="text3",
        ).pack(side=tk.LEFT, padx=12)

        self._ws_refresh_tree()

    def _ws_add(self) -> None:
        name = self._ws_name.get().strip()
        url  = self._ws_url.get().strip()
        if not name or not url:
            return
        self.website_list.append(f"{name}|{url}|0")
        self._save_config()
        self._ws_name.set("")
        self._ws_url.set("")
        self._ws_refresh_tree()
        self._refresh_site_combo()
        self.log(f"Website '{name}' added.", "success")

    def _ws_delete(self) -> None:
        sel = self._ws_tree.selection()
        if not sel:
            return
        idx = self._ws_tree.index(sel[0])
        if 0 <= idx < len(self.website_list):
            removed = self.website_list.pop(idx)
            name = removed.split("|")[0]
            self._save_config()
            self._ws_refresh_tree()
            self._refresh_site_combo()
            self.log(f"Website '{name}' deleted.", "warning")

    def _ws_refresh_tree(self) -> None:
        if not hasattr(self, "_ws_tree"):
            return
        self._ws_tree.delete(*self._ws_tree.get_children())
        for i, site in enumerate(self.website_list):
            parts = site.split("|", 2)
            name  = parts[0] if len(parts) > 0 else ""
            url   = parts[1] if len(parts) > 1 else ""
            opens = parts[2] if len(parts) > 2 else "0"
            tag = "even" if i % 2 == 0 else "odd"
            self._ws_tree.insert("", "end", values=(name, url, opens), tags=(tag,))

    def _ws_on_double_click(self, _event: object) -> None:
        sel = self._ws_tree.selection()
        if not sel:
            return
        vals = self._ws_tree.item(sel[0], "values")
        if len(vals) >= 2:
            self._launcher_name.set(vals[0])
            self._launcher_url.set(vals[1])
            self._switch_tab("launcher")

    # ======================================================================
    # PAGE 3 — Proxies
    # ======================================================================

    def _build_proxies_page(self, parent: tk.Frame) -> None:
        """Build the Proxies page."""
        c = self._get_colors()

        # ── Top row ────────────────────────────────────────────────────────
        top_row = self._tw(tk.Frame(parent), background="bg")
        top_row.pack(fill=tk.X, padx=16, pady=(12, 4))

        self._px_entry_var = tk.StringVar()
        self._tw(
            tk.Entry(top_row, textvariable=self._px_entry_var, font=f(12),
                     relief=tk.FLAT, bd=0),
            background="card", foreground="text", insertbackground="text",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 8))

        add_btn = self._tw(
            tk.Label(top_row, text="Add", font=f(12, "bold"),
                     padx=12, pady=6, cursor="hand2"),
            background="accent", foreground="card",
        )
        add_btn.pack(side=tk.LEFT, padx=(0, 8))
        add_btn.bind("<Button-1>", lambda _e: self._px_add())

        mass_btn = self._tw(
            tk.Label(top_row, text="⚡ Mass Import", font=f(12, "bold"),
                     padx=12, pady=6, cursor="hand2"),
            background="warning", foreground="card",
        )
        mass_btn.pack(side=tk.LEFT)
        mass_btn.bind("<Button-1>", lambda _e: self._px_mass_import())

        # ── Info bar ───────────────────────────────────────────────────────
        info_bar = self._tw(tk.Frame(parent), background="bg")
        info_bar.pack(fill=tk.X, padx=16, pady=(0, 4))

        self._px_count_var = tk.StringVar(value="0 proxies")
        self._tw(
            tk.Label(info_bar, textvariable=self._px_count_var, font=f(11), anchor="w"),
            background="bg", foreground="text2",
        ).pack(side=tk.LEFT)
        self._tw(
            tk.Label(
                info_bar,
                text="  ·  Formats: host:port  host:port:user:pass  "
                     "user:pass@host:port  scheme://...",
                font=f(10), anchor="w",
            ),
            background="bg", foreground="text3",
        ).pack(side=tk.LEFT)

        # ── Treeview ───────────────────────────────────────────────────────
        tree_frame = self._tw(tk.Frame(parent), background="bg")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        cols = ("#", "Address", "Protocol", "Auth")
        self._px_tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", selectmode="browse"
        )
        for col in cols:
            self._px_tree.heading(col, text=col)
        self._px_tree.column("#",        width=40,  stretch=False)
        self._px_tree.column("Address",  width=280, stretch=True)
        self._px_tree.column("Protocol", width=80,  stretch=False)
        self._px_tree.column("Auth",     width=80,  stretch=False)
        self._px_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        px_sb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._px_tree.yview)
        px_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._px_tree.configure(yscrollcommand=px_sb.set)

        self._px_tree.tag_configure("odd",  background=c["card"])
        self._px_tree.tag_configure("even", background=c["surface2"])

        # ── Footer ─────────────────────────────────────────────────────────
        footer = self._tw(tk.Frame(parent), background="bg")
        footer.pack(fill=tk.X, padx=16, pady=(4, 12))

        rm_btn = self._tw(
            tk.Label(footer, text="Remove Selected", font=f(12, "bold"),
                     padx=12, pady=6, cursor="hand2"),
            background="danger", foreground="card",
        )
        rm_btn.pack(side=tk.LEFT, padx=(0, 8))
        rm_btn.bind("<Button-1>", lambda _e: self._px_remove())

        exp_btn = self._tw(
            tk.Label(footer, text="Export", font=f(12, "bold"),
                     padx=12, pady=6, cursor="hand2"),
            background="surface2", foreground="text",
        )
        exp_btn.pack(side=tk.LEFT)
        exp_btn.bind("<Button-1>", lambda _e: self._px_export())

        self._px_refresh_tree()

    def _px_add(self) -> None:
        raw = self._px_entry_var.get().strip()
        if not raw:
            return
        ok, result = normalize_proxy(raw)
        if not ok:
            self.log(f"Invalid proxy: {result}", "error")
            return
        if result in self.proxy_list:
            self.log("Proxy already in list.", "warning")
            return
        self.proxy_list.append(result)
        self._save_config()
        self._px_entry_var.set("")
        self._px_refresh_tree()
        self._update_proxy_preview()
        self._update_stats()
        self.log(f"Proxy added: {result}", "success")

    def _px_remove(self) -> None:
        sel = self._px_tree.selection()
        if not sel:
            return
        idx = self._px_tree.index(sel[0])
        if 0 <= idx < len(self.proxy_list):
            removed = self.proxy_list.pop(idx)
            self._save_config()
            self._px_refresh_tree()
            self._update_proxy_preview()
            self._update_stats()
            self.log(f"Proxy removed: {removed}", "warning")

    def _px_export(self) -> None:
        import tkinter.filedialog as fd
        path = fd.asksaveasfilename(
            title="Export Proxies",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            try:
                pathlib.Path(path).write_text(
                    "\n".join(self.proxy_list), encoding="utf-8"
                )
                self.log(f"Exported {len(self.proxy_list)} proxies to {path}", "success")
            except Exception as exc:
                self.log(f"Export failed: {exc}", "error")

    def _px_refresh_tree(self) -> None:
        if not hasattr(self, "_px_tree"):
            return
        self._px_tree.delete(*self._px_tree.get_children())
        for i, proxy in enumerate(self.proxy_list):
            parsed = urlparse(proxy)
            proto  = parsed.scheme or "http"
            host   = f"{parsed.hostname}:{parsed.port}"
            auth   = "Yes" if parsed.username else "No"
            tag    = "even" if i % 2 == 0 else "odd"
            self._px_tree.insert("", "end", values=(i + 1, host, proto, auth), tags=(tag,))
        if hasattr(self, "_px_count_var"):
            self._px_count_var.set(f"{len(self.proxy_list)} proxies")

    def _px_mass_import(self) -> None:
        """Open the Mass Import dialog with a dim overlay."""
        c = self._get_colors()

        overlay = tk.Frame(self.root, bg="#000000")
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lower()
        try:
            overlay.lift()
        except Exception:
            pass

        dlg = tk.Toplevel(self.root)
        dlg.title("Mass Import Proxies")
        dlg.resizable(True, True)
        dlg.grab_set()
        dlg.geometry("560x480")
        dlg.configure(background=c["bg"])

        def _on_close():
            overlay.destroy()
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _on_close)

        tk.Label(
            dlg, text="Paste proxies — one per line", font=f(13, "bold"),
            background=c["bg"], foreground=c["text"], pady=8,
        ).pack()

        txt_frame = tk.Frame(dlg, background=c["bg"])
        txt_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))

        txt = tk.Text(
            txt_frame, font=fm(11), relief=tk.FLAT, bd=0,
            background=c["card"], foreground=c["text"],
            insertbackground=c["text"], wrap="none", undo=True,
        )
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        txt_sb = ttk.Scrollbar(txt_frame, orient=tk.VERTICAL, command=txt.yview)
        txt_sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.configure(yscrollcommand=txt_sb.set)

        txt.tag_configure("valid",   foreground=c["success"])
        txt.tag_configure("invalid", foreground=c["danger"])

        status_var = tk.StringVar(value="0 valid · 0 invalid")
        tk.Label(
            dlg, textvariable=status_var, font=f(11),
            background=c["bg"], foreground=c["text2"],
        ).pack()

        def _highlight(_event=None):
            txt.tag_remove("valid",   "1.0", "end")
            txt.tag_remove("invalid", "1.0", "end")
            good = bad = 0
            for i, line in enumerate(txt.get("1.0", "end").splitlines(), start=1):
                if not line.strip() or line.strip().startswith("#"):
                    continue
                ok, _ = normalize_proxy(line)
                tag   = "valid" if ok else "invalid"
                txt.tag_add(tag, f"{i}.0", f"{i}.end")
                if ok:
                    good += 1
                else:
                    bad += 1
            status_var.set(f"{good} valid · {bad} invalid")

        txt.bind("<KeyRelease>", _highlight)

        btn_row = tk.Frame(dlg, background=c["bg"])
        btn_row.pack(fill=tk.X, padx=12, pady=(4, 12))

        def _paste():
            try:
                clip = self.root.clipboard_get()
                txt.delete("1.0", "end")
                txt.insert("1.0", clip)
                _highlight()
            except Exception:
                pass

        def _load_file():
            import tkinter.filedialog as fd2
            path = fd2.askopenfilename(
                title="Load proxy file",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if path:
                try:
                    content = pathlib.Path(path).read_text(encoding="utf-8")
                    txt.delete("1.0", "end")
                    txt.insert("1.0", content)
                    _highlight()
                except Exception as exc:
                    self.log(f"Load file error: {exc}", "error")

        def _import():
            raw = txt.get("1.0", "end")
            valid, errors = parse_proxy_lines(raw)
            added = 0
            for p in valid:
                if p not in self.proxy_list:
                    self.proxy_list.append(p)
                    added += 1
            self._save_config()
            self._px_refresh_tree()
            self._update_proxy_preview()
            self._update_stats()
            lvl = "success" if not errors else "warning"
            self.log(f"Mass import: {added} added, {len(errors)} errors.", lvl)
            _on_close()

        for text_, cmd, bg_key in [
            ("Paste from Clipboard", _paste,     "surface2"),
            ("Load File",            _load_file, "surface2"),
            ("Import",               _import,    "accent"),
        ]:
            bg = c[bg_key]
            fg = c["card"] if bg_key == "accent" else c["text"]
            lbl = tk.Label(
                btn_row, text=text_, font=f(12, "bold"),
                padx=12, pady=6, cursor="hand2",
                background=bg, foreground=fg,
            )
            lbl.pack(side=tk.LEFT, padx=(0, 8))
            lbl.bind("<Button-1>", lambda _e, cmd2=cmd: cmd2())

    # ======================================================================
    # PAGE 4 — Log
    # ======================================================================

    def _build_log_page(self, parent: tk.Frame) -> None:
        """Build the Log page."""
        c = self._get_colors()

        header = self._tw(tk.Frame(parent), background="bg")
        header.pack(fill=tk.X, padx=16, pady=(12, 4))

        self._tw(
            tk.Label(header, text="Session Log", font=f(13, "bold"), anchor="w"),
            background="bg", foreground="text",
        ).pack(side=tk.LEFT)

        clear_btn = self._tw(
            tk.Label(header, text="Clear", font=f(11), padx=10, pady=4, cursor="hand2"),
            background="surface2", foreground="text",
        )
        clear_btn.pack(side=tk.RIGHT)
        clear_btn.bind("<Button-1>", lambda _e: self._log_clear())

        txt_frame = self._tw(tk.Frame(parent), background="bg")
        txt_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        self._log_text = tk.Text(
            txt_frame, font=fm(11), relief=tk.FLAT, bd=0,
            state=tk.DISABLED, wrap="none",
            background=c["card"], foreground=c["text"],
        )
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        log_sb = ttk.Scrollbar(txt_frame, orient=tk.VERTICAL, command=self._log_text.yview)
        log_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.configure(yscrollcommand=log_sb.set)

        self._log_text.tag_configure("ts",      foreground=c["text3"])
        self._log_text.tag_configure("info",    foreground=c["text"])
        self._log_text.tag_configure("success", foreground=c["success"])
        self._log_text.tag_configure("warning", foreground=c["warning"])
        self._log_text.tag_configure("error",   foreground=c["danger"])

    def log(self, msg: str, level: str = "info") -> None:
        """Append *msg* to the log page. Safe to call from any thread."""
        self.root.after(0, lambda: self._log_append(msg, level))

    def _log_append(self, msg: str, level: str = "info") -> None:
        if not hasattr(self, "_log_text"):
            return
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.insert("end", f"[{ts}] ", ("ts",))
        self._log_text.insert("end", f"{msg}\n", (level,))
        self._log_text.configure(state=tk.DISABLED)
        self._log_text.see("end")

    def _log_clear(self) -> None:
        if hasattr(self, "_log_text"):
            self._log_text.configure(state=tk.NORMAL)
            self._log_text.delete("1.0", "end")
            self._log_text.configure(state=tk.DISABLED)

    # ======================================================================
    # Placeholder helper for search entry
    # ======================================================================

    def _set_placeholder(self, entry: tk.Entry, placeholder: str) -> None:
        """Show greyed placeholder text; clear on focus, restore on blur."""
        c = self._get_colors()

        def _focus_in(_e: tk.Event) -> None:
            if entry.get() == placeholder:
                entry.delete(0, tk.END)
                try:
                    entry.configure(foreground=c["text"])
                except tk.TclError:
                    pass

        def _focus_out(_e: tk.Event) -> None:
            if not entry.get():
                entry.insert(0, placeholder)
                try:
                    entry.configure(foreground=c["text3"])
                except tk.TclError:
                    pass

        entry.insert(0, placeholder)
        try:
            entry.configure(foreground=c["text3"])
        except tk.TclError:
            pass
        entry.bind("<FocusIn>",  _focus_in)
        entry.bind("<FocusOut>", _focus_out)

    # ======================================================================
    # Log-queue drain  (called every 100 ms via root.after)
    # ======================================================================

    def _drain_log(self) -> None:
        """Forward queued backend log messages to the Log page and Python logger."""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._log_append(msg, "info")
                logger.info("[session] %s", msg)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = tk.Tk()
    app  = BrowserGuardApp(root)  # noqa: F841
    root.mainloop()


if __name__ == "__main__":
    main()
