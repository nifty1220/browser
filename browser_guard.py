"""browser_guard: Utilities for controlling and validating browser access."""

import logging
import re
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from tkinter import ttk
from typing import Deque, List, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum number of requests allowed per second per origin
RATE_LIMIT_RPS = 10

# Hard timeout (seconds) for any single page navigation
NAV_TIMEOUT_SECONDS = 30

# Schemes considered safe to navigate to
ALLOWED_SCHEMES = {"http", "https"}

# Domains that are always blocked regardless of other rules (mutable so the
# Websites tab can add/remove entries at runtime)
BLOCKED_DOMAINS: set[str] = set()

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def extract_domain(url: str) -> Optional[str]:
    """Return the netloc of *url*, or None if the URL cannot be parsed."""
    try:
        return urlparse(url).netloc or None
    except Exception:
        return None


def is_allowed_scheme(url: str) -> bool:
    """Return True if *url* uses a scheme in ALLOWED_SCHEMES."""
    try:
        scheme = urlparse(url).scheme.lower()
        return scheme in ALLOWED_SCHEMES
    except Exception:
        return False


def is_blocked_domain(url: str) -> bool:
    """Return True if the domain of *url* is in BLOCKED_DOMAINS."""
    domain = extract_domain(url)
    if domain is None:
        return False
    # Strip port if present
    host = domain.split(":")[0].lower()
    return host in BLOCKED_DOMAINS


def is_safe_url(url: str) -> bool:
    """Return True only when *url* passes all basic safety checks."""
    if not is_allowed_scheme(url):
        logger.debug("Blocked URL with disallowed scheme: %s", url)
        return False
    if is_blocked_domain(url):
        logger.debug("Blocked URL from blocked domain: %s", url)
        return False
    return True


def sanitize_url(url: str) -> str:
    """Strip whitespace and remove embedded newlines/carriage-returns from *url*."""
    return re.sub(r"[\r\n\t]", "", url.strip())


def current_timestamp() -> float:
    """Return the current UNIX timestamp as a float."""
    return time.time()


# ---------------------------------------------------------------------------
# ProxyTunnel
# ---------------------------------------------------------------------------


class ProxyTunnel:
    """Represents a single proxy connection used to route browser traffic.

    Tracks liveness and request counts so that callers can rotate or
    retire tunnels that have exceeded their quota or gone stale.
    """

    def __init__(self, host: str, port: int, *, username: str = "", password: str = "") -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password

        self._connected: bool = False
        self._request_count: int = 0
        self._opened_at: Optional[float] = None
        self._closed_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def address(self) -> str:
        """Return ``host:port`` string."""
        return f"{self.host}:{self.port}"

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def uptime(self) -> Optional[float]:
        """Seconds since the tunnel was opened, or None if never opened."""
        if self._opened_at is None:
            return None
        end = self._closed_at if self._closed_at is not None else current_timestamp()
        return end - self._opened_at

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Mark the tunnel as connected."""
        if self._connected:
            return
        self._connected = True
        self._opened_at = current_timestamp()
        self._closed_at = None
        logger.debug("ProxyTunnel opened: %s", self.address)

    def close(self) -> None:
        """Mark the tunnel as disconnected."""
        if not self._connected:
            return
        self._connected = False
        self._closed_at = current_timestamp()
        logger.debug("ProxyTunnel closed: %s (requests=%d)", self.address, self._request_count)

    # ------------------------------------------------------------------
    # Request tracking
    # ------------------------------------------------------------------

    def record_request(self) -> None:
        """Increment the request counter for this tunnel."""
        self._request_count += 1

    def reset_count(self) -> None:
        """Reset the request counter to zero."""
        self._request_count = 0

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        status = "connected" if self._connected else "closed"
        return f"<ProxyTunnel {self.address} {status} requests={self._request_count}>"


# ---------------------------------------------------------------------------
# BrowserSession
# ---------------------------------------------------------------------------


class SessionState(Enum):
    IDLE = auto()
    ACTIVE = auto()
    CLOSED = auto()


@dataclass
class NavigationRecord:
    url: str
    timestamp: float
    success: bool


class BrowserSession:
    """Manages the lifecycle and guard policies for a single browser session.

    Responsibilities:
    - Enforce URL safety checks before every navigation.
    - Apply per-session rate limiting (RATE_LIMIT_RPS).
    - Track navigation history.
    - Optionally route traffic through a ProxyTunnel.
    """

    def __init__(
        self,
        session_id: str,
        *,
        proxy: Optional[ProxyTunnel] = None,
        rate_limit_rps: int = RATE_LIMIT_RPS,
        nav_timeout: float = NAV_TIMEOUT_SECONDS,
    ) -> None:
        self.session_id = session_id
        self.proxy = proxy
        self.rate_limit_rps = rate_limit_rps
        self.nav_timeout = nav_timeout

        self._state: SessionState = SessionState.IDLE
        self._history: List[NavigationRecord] = []
        # Sliding window of request timestamps for rate limiting
        self._request_times: Deque[float] = deque()
        self._created_at: float = current_timestamp()
        self._closed_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state == SessionState.ACTIVE

    @property
    def history(self) -> List[NavigationRecord]:
        return list(self._history)

    @property
    def uptime(self) -> Optional[float]:
        if self._state == SessionState.IDLE:
            return None
        end = self._closed_at if self._closed_at is not None else current_timestamp()
        return end - self._created_at

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Transition the session from IDLE to ACTIVE."""
        if self._state != SessionState.IDLE:
            raise RuntimeError(f"Cannot start session in state {self._state.name}")
        if self.proxy is not None:
            self.proxy.open()
        self._state = SessionState.ACTIVE
        logger.info("BrowserSession started: %s", self.session_id)

    def close(self) -> None:
        """Close the session and any associated proxy tunnel."""
        if self._state == SessionState.CLOSED:
            return
        if self.proxy is not None:
            self.proxy.close()
        self._state = SessionState.CLOSED
        self._closed_at = current_timestamp()
        logger.info(
            "BrowserSession closed: %s (navigations=%d)", self.session_id, len(self._history)
        )

    # ------------------------------------------------------------------
    # Navigation guard
    # ------------------------------------------------------------------

    def can_navigate(self, url: str) -> bool:
        """Return True if *url* is permitted under current guard policies.

        Checks (in order):
        1. Session must be ACTIVE.
        2. URL must pass is_safe_url().
        3. Rate limit must not be exceeded.
        """
        if self._state != SessionState.ACTIVE:
            logger.warning("Navigation denied — session not active (%s)", self._state.name)
            return False

        clean = sanitize_url(url)
        if not is_safe_url(clean):
            return False

        if not self._within_rate_limit():
            logger.warning("Navigation denied — rate limit exceeded for session %s", self.session_id)
            return False

        return True

    def navigate(self, url: str) -> bool:
        """Attempt to record a navigation to *url*.

        Returns True if the navigation was allowed and recorded, False otherwise.
        In a real implementation the caller would drive the actual browser
        after receiving True.
        """
        allowed = self.can_navigate(url)
        clean = sanitize_url(url)
        self._history.append(
            NavigationRecord(url=clean, timestamp=current_timestamp(), success=allowed)
        )
        if allowed:
            self._record_request_time()
            if self.proxy is not None:
                self.proxy.record_request()
            logger.debug("Navigation allowed: %s → %s", self.session_id, clean)
        return allowed

    # ------------------------------------------------------------------
    # Rate limiting (token-bucket via sliding window)
    # ------------------------------------------------------------------

    def _within_rate_limit(self) -> bool:
        now = current_timestamp()
        window_start = now - 1.0  # 1-second sliding window
        # Drop timestamps outside the window
        while self._request_times and self._request_times[0] < window_start:
            self._request_times.popleft()
        return len(self._request_times) < self.rate_limit_rps

    def _record_request_time(self) -> None:
        self._request_times.append(current_timestamp())

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<BrowserSession {self.session_id!r} state={self._state.name} "
            f"navigations={len(self._history)}>"
        )

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# BrowserGuardApp
# ---------------------------------------------------------------------------

# Sidebar width in pixels
SIDEBAR_WIDTH = 200

# Toolbar height in pixels
TOOLBAR_HEIGHT = 36

# Application title shown in the window title bar
APP_TITLE = "BrowserGuard"

# ---------------------------------------------------------------------------
# Theme palettes
# ---------------------------------------------------------------------------

_LIGHT_PALETTE: dict = {
    "bg":           "#f0f0f0",
    "fg":           "#1a1a1a",
    "card_bg":      "#ffffff",
    "entry_bg":     "#ffffff",
    "toolbar_bg":   "#e0e0e0",
    "text_bg":      "#ffffff",
    "badge_active": "#34a853",
    "badge_idle":   "#e8a000",
    "badge_closed": "#9e9e9e",
    "select_bg":    "#0078d4",
    "select_fg":    "#ffffff",
}

_DARK_PALETTE: dict = {
    "bg":           "#1e1e2e",
    "fg":           "#cdd6f4",
    "card_bg":      "#2a2a3e",
    "entry_bg":     "#313244",
    "toolbar_bg":   "#181825",
    "text_bg":      "#181825",
    "badge_active": "#a6e3a1",
    "badge_idle":   "#f9e2af",
    "badge_closed": "#585b70",
    "select_bg":    "#89b4fa",
    "select_fg":    "#1e1e2e",
}


# ---------------------------------------------------------------------------
# SessionCard
# ---------------------------------------------------------------------------


class SessionCard(ttk.Frame):
    """Compact card widget summarising one BrowserSession.

    Displays the session ID, a coloured state badge, proxy address,
    navigation count, and Stop / Remove action buttons.
    """

    _BADGE_KEYS = {
        SessionState.ACTIVE: "badge_active",
        SessionState.IDLE:   "badge_idle",
        SessionState.CLOSED: "badge_closed",
    }

    def __init__(
        self,
        parent: tk.Misc,
        session: BrowserSession,
        *,
        on_stop=None,
        on_remove=None,
    ) -> None:
        super().__init__(parent, relief=tk.RIDGE, borderwidth=1, padding=6)
        self.session = session
        self._on_stop_cb = on_stop
        self._on_remove_cb = on_remove
        self._current_palette: dict = _LIGHT_PALETTE
        self._build()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # Top row: session ID + state badge
        top = ttk.Frame(self)
        top.pack(fill=tk.X)

        self._id_label = ttk.Label(
            top, text=self.session.session_id, font=("TkDefaultFont", 9, "bold")
        )
        self._id_label.pack(side=tk.LEFT)

        self._badge = tk.Label(top, font=("TkDefaultFont", 8), relief=tk.FLAT, padx=4, pady=1)
        self._badge.pack(side=tk.RIGHT)

        # Middle row: proxy address + nav count
        mid = ttk.Frame(self)
        mid.pack(fill=tk.X, pady=(2, 0))

        proxy_text = self.session.proxy.address if self.session.proxy else "no proxy"
        ttk.Label(mid, text=f"via {proxy_text}", font=("TkFixedFont", 8)).pack(side=tk.LEFT)

        self._nav_var = tk.StringVar()
        ttk.Label(mid, textvariable=self._nav_var, font=("TkFixedFont", 8)).pack(side=tk.RIGHT)

        # Bottom row: action buttons
        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, pady=(4, 0))

        self._stop_btn = ttk.Button(btns, text="Stop", width=6, command=self._on_stop)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._remove_btn = ttk.Button(btns, text="Remove", width=7, command=self._on_remove)
        self._remove_btn.pack(side=tk.LEFT)

        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Redraw dynamic fields from the current session state."""
        state = self.session.state
        self._nav_var.set(f"{len(self.session.history)} nav")
        self._stop_btn.configure(
            state=tk.NORMAL if state == SessionState.ACTIVE else tk.DISABLED
        )
        badge_key = self._BADGE_KEYS.get(state, "badge_closed")
        self._badge.configure(
            text=state.name,
            background=self._current_palette[badge_key],
            foreground="#ffffff",
        )

    def apply_palette(self, palette: dict) -> None:
        """Re-colour widgets that cannot be reached via ttk.Style."""
        self._current_palette = palette
        self.refresh()

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_stop(self) -> None:
        if self._on_stop_cb:
            self._on_stop_cb(self.session)
        self.refresh()

    def _on_remove(self) -> None:
        if self._on_remove_cb:
            self._on_remove_cb(self.session)


# ---------------------------------------------------------------------------
# SettingsModal
# ---------------------------------------------------------------------------


class SettingsModal(tk.Toplevel):
    """Modal dialog for application-wide default settings and theme choice."""

    def __init__(self, parent: tk.Misc, app: "BrowserGuardApp") -> None:
        super().__init__(parent)
        self._app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._build()
        self._centre()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)

        # Session ID prefix
        ttk.Label(outer, text="Session ID prefix:").grid(
            row=0, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self._prefix_var = tk.StringVar(
            value=self._app._settings.get("session_prefix", "session")
        )
        ttk.Entry(outer, textvariable=self._prefix_var, width=22).grid(
            row=0, column=1, sticky="ew"
        )

        # Default rate limit
        ttk.Label(outer, text="Default rate limit (req/s):").grid(
            row=1, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self._rps_var = tk.IntVar(
            value=self._app._settings.get("rate_limit_rps", RATE_LIMIT_RPS)
        )
        ttk.Spinbox(outer, from_=1, to=100, textvariable=self._rps_var, width=8).grid(
            row=1, column=1, sticky="w"
        )

        # Default nav timeout
        ttk.Label(outer, text="Default nav timeout (s):").grid(
            row=2, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self._timeout_var = tk.DoubleVar(
            value=self._app._settings.get("nav_timeout", NAV_TIMEOUT_SECONDS)
        )
        ttk.Spinbox(outer, from_=1, to=300, textvariable=self._timeout_var, width=8).grid(
            row=2, column=1, sticky="w"
        )

        # Theme selection
        ttk.Label(outer, text="Theme:").grid(
            row=3, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self._theme_var = tk.StringVar(value="dark" if self._app._dark_mode else "light")
        theme_frame = ttk.Frame(outer)
        theme_frame.grid(row=3, column=1, sticky="w")
        ttk.Radiobutton(theme_frame, text="Light", variable=self._theme_var, value="light").pack(
            side=tk.LEFT
        )
        ttk.Radiobutton(theme_frame, text="Dark", variable=self._theme_var, value="dark").pack(
            side=tk.LEFT, padx=(12, 0)
        )

        ttk.Separator(outer, orient=tk.HORIZONTAL).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(12, 8)
        )

        # OK / Cancel
        btn_row = ttk.Frame(outer)
        btn_row.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_row, text="OK", command=self._on_ok).pack(side=tk.RIGHT)

    def _centre(self) -> None:
        self.update_idletasks()
        rx, ry = self.master.winfo_rootx(), self.master.winfo_rooty()
        rw, rh = self.master.winfo_width(), self.master.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{rx + (rw - w) // 2}+{ry + (rh - h) // 2}")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_ok(self) -> None:
        self._app._settings["session_prefix"] = self._prefix_var.get().strip() or "session"
        self._app._settings["rate_limit_rps"] = self._rps_var.get()
        self._app._settings["nav_timeout"] = self._timeout_var.get()

        want_dark = self._theme_var.get() == "dark"
        if want_dark != self._app._dark_mode:
            self._app.toggle_theme()

        # Push updated defaults back to the Launcher tab
        self._app._launch_rps_var.set(self._app._settings["rate_limit_rps"])
        self._app._launch_timeout_var.set(self._app._settings["nav_timeout"])
        prefix = self._app._settings["session_prefix"]
        self._app._launch_id_var.set(f"{prefix}-{self._app._session_count + 1}")

        logger.info("Settings saved: %s", self._app._settings)
        self.destroy()


class BrowserGuardApp:
    """Top-level Tk application for BrowserGuard.

    Layout
    ------
    ┌─────────────────────────────────────────────────────────┐
    │  [URL entry]  Go  Stop  Refresh    🌙 Dark  ⚙ Settings  │
    ├──────────────┬──────────────────────────────────────────┤
    │  Sessions [+]│ [Launcher][Websites][Proxies][Log]        │
    │  ┌─────────┐ │  tab content                             │
    │  │SessionCard│                                          │
    │  │SessionCard│                                          │
    │  └─────────┘ │                                          │
    └──────────────┴──────────────────────────────────────────┘
    """

    def __init__(self, root: tk.Tk, session: Optional[BrowserSession] = None) -> None:
        self.root = root
        self.session = session  # currently active/selected session

        # App-wide state
        self._dark_mode: bool = False
        self._session_count: int = 0
        self._sessions: dict = {}        # session_id -> BrowserSession
        self._session_cards: dict = {}   # session_id -> SessionCard
        self._settings: dict = {
            "session_prefix":  "session",
            "rate_limit_rps":  RATE_LIMIT_RPS,
            "nav_timeout":     NAV_TIMEOUT_SECONDS,
        }

        self._setup_window()
        self._build_toolbar()
        self._build_main_area()
        self.apply_theme(dark=False)

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        """Configure the root Tk window."""
        self.root.title(APP_TITLE)
        self.root.minsize(800, 600)
        self.root.resizable(True, True)

        # Let the content column expand when the window is resized
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)  # row 0 = toolbar, row 1 = main area

        logger.debug("Window configured: %s", APP_TITLE)

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        """Create the toolbar row at the top of the window."""
        self.toolbar = tk.Frame(
            self.root,
            height=TOOLBAR_HEIGHT,
            bd=1,
            relief=tk.RAISED,
        )
        self.toolbar.grid(row=0, column=0, sticky="ew")
        self.toolbar.grid_propagate(False)

        # URL entry field
        self._url_var = tk.StringVar()
        self._url_entry = ttk.Entry(self.toolbar, textvariable=self._url_var, width=60)
        self._url_entry.pack(side=tk.LEFT, padx=(8, 4), pady=4, fill=tk.X, expand=True)
        self._url_entry.bind("<Return>", self._on_navigate)

        # Navigate button
        self._nav_btn = ttk.Button(self.toolbar, text="Go", command=self._on_navigate)
        self._nav_btn.pack(side=tk.LEFT, padx=(0, 4), pady=4)

        # Stop button
        self._stop_btn = ttk.Button(self.toolbar, text="Stop", command=self._on_stop)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 4), pady=4)

        # Refresh button
        self._refresh_btn = ttk.Button(self.toolbar, text="Refresh", command=self._on_refresh)
        self._refresh_btn.pack(side=tk.LEFT, padx=(0, 4), pady=4)

        # Settings button (right-aligned)
        self._settings_btn = ttk.Button(
            self.toolbar, text="⚙ Settings", command=self._open_settings
        )
        self._settings_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=4)

        # Theme toggle button (right-aligned)
        self._theme_btn = ttk.Button(
            self.toolbar, text="🌙 Dark", width=9, command=self.toggle_theme
        )
        self._theme_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=4)

        logger.debug("Toolbar built")

    # ------------------------------------------------------------------
    # Main area (sidebar + content)
    # ------------------------------------------------------------------

    def _build_main_area(self) -> None:
        """Create the paned container that holds the sidebar and content area."""
        self._pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self._pane.grid(row=1, column=0, sticky="nsew")

        self._build_sidebar()
        self._build_content_area()

    def _build_sidebar(self) -> None:
        """Create the sidebar with a scrollable area of SessionCard widgets."""
        self.sidebar = ttk.Frame(self._pane, width=SIDEBAR_WIDTH)
        self.sidebar.pack_propagate(False)
        self._pane.add(self.sidebar, weight=0)

        # Header row: label + quick-add button
        hdr = ttk.Frame(self.sidebar)
        hdr.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(hdr, text="Sessions", font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(hdr, text="+", width=3, command=self._on_new_session).pack(side=tk.RIGHT)

        # Scrollable card area
        sf = ttk.Frame(self.sidebar)
        sf.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        sf.columnconfigure(0, weight=1)
        sf.rowconfigure(0, weight=1)

        self._cards_canvas = tk.Canvas(sf, highlightthickness=0)
        self._cards_canvas.grid(row=0, column=0, sticky="nsew")

        cards_sb = ttk.Scrollbar(sf, orient=tk.VERTICAL, command=self._cards_canvas.yview)
        cards_sb.grid(row=0, column=1, sticky="ns")
        self._cards_canvas.configure(yscrollcommand=cards_sb.set)

        self._cards_inner = ttk.Frame(self._cards_canvas)
        self._cards_window = self._cards_canvas.create_window(
            (0, 0), window=self._cards_inner, anchor="nw"
        )

        self._cards_inner.bind("<Configure>", self._on_cards_frame_configure)
        self._cards_canvas.bind("<Configure>", self._on_cards_canvas_configure)

        logger.debug("Sidebar built with scrollable card area")

    def _build_content_area(self) -> None:
        """Create the right-hand content frame containing the tab notebook."""
        self.content = ttk.Frame(self._pane)
        self._pane.add(self.content, weight=1)

        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

        self._notebook = ttk.Notebook(self.content)
        self._notebook.grid(row=0, column=0, sticky="nsew")

        self._build_tab_launcher()
        self._build_tab_websites()
        self._build_tab_proxies()
        self._build_tab_log()

        logger.debug("Content area built with %d tabs", self._notebook.index("end"))

    # ------------------------------------------------------------------
    # Tab: Launcher
    # ------------------------------------------------------------------

    def _build_tab_launcher(self) -> None:
        """Build the Launcher tab — start/stop sessions and set guard options."""
        tab = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(tab, text="Launcher")

        # -- Session identity ------------------------------------------
        id_frame = ttk.LabelFrame(tab, text="Session", padding=8)
        id_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(id_frame, text="Session ID:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._launch_id_var = tk.StringVar(value="session-1")
        ttk.Entry(id_frame, textvariable=self._launch_id_var, width=30).grid(
            row=0, column=1, sticky="ew"
        )
        id_frame.columnconfigure(1, weight=1)

        # -- Guard options ---------------------------------------------
        opt_frame = ttk.LabelFrame(tab, text="Guard Options", padding=8)
        opt_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(opt_frame, text="Rate limit (req/s):").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self._launch_rps_var = tk.IntVar(value=RATE_LIMIT_RPS)
        ttk.Spinbox(opt_frame, from_=1, to=100, textvariable=self._launch_rps_var, width=8).grid(
            row=0, column=1, sticky="w"
        )

        ttk.Label(opt_frame, text="Nav timeout (s):").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(4, 0)
        )
        self._launch_timeout_var = tk.DoubleVar(value=NAV_TIMEOUT_SECONDS)
        ttk.Spinbox(
            opt_frame, from_=1, to=300, textvariable=self._launch_timeout_var, width=8
        ).grid(row=1, column=1, sticky="w", pady=(4, 0))

        self._launch_use_proxy_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame, text="Use proxy tunnel", variable=self._launch_use_proxy_var
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # -- Action buttons -------------------------------------------
        btn_frame = ttk.Frame(tab)
        btn_frame.pack(anchor="w", pady=(4, 0))

        self._launch_start_btn = ttk.Button(
            btn_frame, text="Start Session", command=self._on_launch_start
        )
        self._launch_start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._launch_stop_btn = ttk.Button(
            btn_frame, text="Stop Session", command=self._on_launch_stop, state=tk.DISABLED
        )
        self._launch_stop_btn.pack(side=tk.LEFT)

        # -- Status label ---------------------------------------------
        self._launch_status_var = tk.StringVar(value="No session running.")
        ttk.Label(tab, textvariable=self._launch_status_var, foreground="gray").pack(
            anchor="w", pady=(8, 0)
        )

        logger.debug("Tab 'Launcher' built")

    # ------------------------------------------------------------------
    # Tab: Websites
    # ------------------------------------------------------------------

    def _build_tab_websites(self) -> None:
        """Build the Websites tab — manage allowed/blocked domain lists."""
        tab = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(tab, text="Websites")

        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(2, weight=1)
        tab.rowconfigure(1, weight=1)

        # -- Allowed column -------------------------------------------
        ttk.Label(tab, text="Allowed Domains", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self._allowed_listbox = tk.Listbox(tab, selectmode=tk.SINGLE)
        self._allowed_listbox.grid(row=1, column=0, sticky="nsew")

        allowed_btn_frame = ttk.Frame(tab)
        allowed_btn_frame.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._allowed_entry_var = tk.StringVar()
        ttk.Entry(allowed_btn_frame, textvariable=self._allowed_entry_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        ttk.Button(allowed_btn_frame, text="Add", command=self._on_allowed_add).pack(side=tk.LEFT)
        ttk.Button(allowed_btn_frame, text="Remove", command=self._on_allowed_remove).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        # Spacer column
        ttk.Separator(tab, orient=tk.VERTICAL).grid(
            row=0, column=1, rowspan=3, sticky="ns", padx=10
        )

        # -- Blocked column -------------------------------------------
        ttk.Label(tab, text="Blocked Domains", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=2, sticky="w", pady=(0, 4)
        )
        self._blocked_listbox = tk.Listbox(tab, selectmode=tk.SINGLE)
        self._blocked_listbox.grid(row=1, column=2, sticky="nsew")

        blocked_btn_frame = ttk.Frame(tab)
        blocked_btn_frame.grid(row=2, column=2, sticky="ew", pady=(4, 0))
        self._blocked_entry_var = tk.StringVar()
        ttk.Entry(blocked_btn_frame, textvariable=self._blocked_entry_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        ttk.Button(blocked_btn_frame, text="Add", command=self._on_blocked_add).pack(side=tk.LEFT)
        ttk.Button(blocked_btn_frame, text="Remove", command=self._on_blocked_remove).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        logger.debug("Tab 'Websites' built")

    # ------------------------------------------------------------------
    # Tab: Proxies
    # ------------------------------------------------------------------

    def _build_tab_proxies(self) -> None:
        """Build the Proxies tab — configure and manage ProxyTunnel entries."""
        tab = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(tab, text="Proxies")

        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        # -- Proxy form ------------------------------------------------
        form = ttk.LabelFrame(tab, text="Add Proxy", padding=8)
        form.pack(fill=tk.X, pady=(0, 8))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="Host:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self._proxy_host_var = tk.StringVar()
        ttk.Entry(form, textvariable=self._proxy_host_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 12)
        )

        ttk.Label(form, text="Port:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self._proxy_port_var = tk.IntVar(value=8080)
        ttk.Spinbox(form, from_=1, to=65535, textvariable=self._proxy_port_var, width=8).grid(
            row=0, column=3, sticky="w"
        )

        ttk.Label(form, text="Username:").grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=(4, 0)
        )
        self._proxy_user_var = tk.StringVar()
        ttk.Entry(form, textvariable=self._proxy_user_var).grid(
            row=1, column=1, sticky="ew", pady=(4, 0), padx=(0, 12)
        )

        ttk.Label(form, text="Password:").grid(
            row=1, column=2, sticky="w", padx=(0, 4), pady=(4, 0)
        )
        self._proxy_pass_var = tk.StringVar()
        ttk.Entry(form, textvariable=self._proxy_pass_var, show="*").grid(
            row=1, column=3, sticky="ew", pady=(4, 0)
        )

        ttk.Button(form, text="Add Proxy", command=self._on_proxy_add).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )

        # -- Proxy list ------------------------------------------------
        ttk.Label(tab, text="Configured Proxies", font=("TkDefaultFont", 9, "bold")).pack(
            anchor="w", pady=(0, 4)
        )

        list_frame = ttk.Frame(tab)
        list_frame.pack(fill=tk.BOTH, expand=True)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        cols = ("address", "status", "requests")
        self._proxy_tree = ttk.Treeview(
            list_frame, columns=cols, show="headings", selectmode="browse"
        )
        self._proxy_tree.heading("address", text="Address")
        self._proxy_tree.heading("status", text="Status")
        self._proxy_tree.heading("requests", text="Requests")
        self._proxy_tree.column("address", width=220)
        self._proxy_tree.column("status", width=90, anchor="center")
        self._proxy_tree.column("requests", width=80, anchor="center")
        self._proxy_tree.grid(row=0, column=0, sticky="nsew")

        proxy_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._proxy_tree.yview)
        self._proxy_tree.configure(yscrollcommand=proxy_scroll.set)
        proxy_scroll.grid(row=0, column=1, sticky="ns")

        ttk.Button(tab, text="Remove Selected", command=self._on_proxy_remove).pack(
            anchor="w", pady=(4, 0)
        )

        logger.debug("Tab 'Proxies' built")

    # ------------------------------------------------------------------
    # Tab: Log
    # ------------------------------------------------------------------

    def _build_tab_log(self) -> None:
        """Build the Log tab — scrollable read-only view of guard activity."""
        tab = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(tab, text="Log")

        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        # -- Log text widget -------------------------------------------
        log_frame = ttk.Frame(tab)
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self._log_text = tk.Text(
            log_frame,
            state=tk.DISABLED,
            wrap=tk.NONE,
            font=("TkFixedFont", 9),
        )
        self._log_text.grid(row=0, column=0, sticky="nsew")

        log_v_scroll = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self._log_text.yview
        )
        self._log_text.configure(yscrollcommand=log_v_scroll.set)
        log_v_scroll.grid(row=0, column=1, sticky="ns")

        log_h_scroll = ttk.Scrollbar(
            log_frame, orient=tk.HORIZONTAL, command=self._log_text.xview
        )
        self._log_text.configure(xscrollcommand=log_h_scroll.set)
        log_h_scroll.grid(row=1, column=0, sticky="ew")

        # Colour tags for log levels
        self._log_text.tag_configure("INFO", foreground="#1a73e8")
        self._log_text.tag_configure("WARNING", foreground="#f4a100")
        self._log_text.tag_configure("ERROR", foreground="#d93025")
        self._log_text.tag_configure("DEBUG", foreground="#5f6368")

        # -- Controls --------------------------------------------------
        ctrl_frame = ttk.Frame(tab)
        ctrl_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self._log_autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl_frame, text="Auto-scroll", variable=self._log_autoscroll_var
        ).pack(side=tk.LEFT)

        ttk.Button(ctrl_frame, text="Clear", command=self._on_log_clear).pack(
            side=tk.RIGHT
        )

        logger.debug("Tab 'Log' built")

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def apply_theme(self, dark: bool) -> None:
        """Apply dark or light colour palette to all widgets."""
        self._dark_mode = dark
        palette = _DARK_PALETTE if dark else _LIGHT_PALETTE

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            ".",
            background=palette["bg"],
            foreground=palette["fg"],
            fieldbackground=palette["entry_bg"],
            selectbackground=palette["select_bg"],
            selectforeground=palette["select_fg"],
            troughcolor=palette["bg"],
            bordercolor=palette["card_bg"],
        )
        for cls_name in (
            "TFrame", "TLabel", "TLabelframe", "TLabelframe.Label",
            "TCheckbutton", "TRadiobutton", "TPanedwindow",
        ):
            style.configure(cls_name, background=palette["bg"], foreground=palette["fg"])
        style.configure("TButton", background=palette["card_bg"], foreground=palette["fg"])
        style.map(
            "TButton",
            background=[("active", palette["select_bg"]), ("disabled", palette["bg"])],
            foreground=[("active", palette["select_fg"]), ("disabled", palette["badge_closed"])],
        )
        style.configure(
            "TEntry", fieldbackground=palette["entry_bg"], foreground=palette["fg"]
        )
        style.configure(
            "TSpinbox",
            fieldbackground=palette["entry_bg"],
            foreground=palette["fg"],
            arrowcolor=palette["fg"],
        )
        style.configure("TNotebook", background=palette["bg"])
        style.configure(
            "TNotebook.Tab",
            background=palette["bg"],
            foreground=palette["fg"],
            padding=(8, 4),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", palette["card_bg"])],
            foreground=[("selected", palette["fg"])],
        )
        style.configure(
            "Treeview",
            background=palette["card_bg"],
            foreground=palette["fg"],
            fieldbackground=palette["card_bg"],
            rowheight=22,
        )
        style.configure(
            "Treeview.Heading", background=palette["bg"], foreground=palette["fg"]
        )
        style.map(
            "Treeview",
            background=[("selected", palette["select_bg"])],
            foreground=[("selected", palette["select_fg"])],
        )

        # Plain tk widgets must be configured directly
        self.root.configure(bg=palette["bg"])
        self.toolbar.configure(bg=palette["toolbar_bg"])

        if hasattr(self, "_cards_canvas"):
            self._cards_canvas.configure(bg=palette["bg"])

        if hasattr(self, "_log_text"):
            self._log_text.configure(
                bg=palette["text_bg"],
                fg=palette["fg"],
                insertbackground=palette["fg"],
                selectbackground=palette["select_bg"],
                selectforeground=palette["select_fg"],
            )
            self._log_text.tag_configure(
                "INFO",    foreground="#89b4fa" if dark else "#1a73e8"
            )
            self._log_text.tag_configure(
                "WARNING", foreground="#f9e2af" if dark else "#f4a100"
            )
            self._log_text.tag_configure(
                "ERROR",   foreground="#f38ba8" if dark else "#d93025"
            )
            self._log_text.tag_configure(
                "DEBUG",   foreground="#a6adc8" if dark else "#5f6368"
            )

        if hasattr(self, "_allowed_listbox"):
            for lb in (self._allowed_listbox, self._blocked_listbox):
                lb.configure(
                    bg=palette["card_bg"],
                    fg=palette["fg"],
                    selectbackground=palette["select_bg"],
                    selectforeground=palette["select_fg"],
                )

        if hasattr(self, "_theme_btn"):
            self._theme_btn.configure(text="☀ Light" if dark else "🌙 Dark")

        for card in self._session_cards.values():
            card.apply_palette(palette)

        logger.debug("Theme applied: %s", "dark" if dark else "light")

    def toggle_theme(self) -> None:
        """Flip between dark and light mode."""
        self.apply_theme(not self._dark_mode)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        """Open the Settings modal."""
        SettingsModal(self.root, self)

    # ------------------------------------------------------------------
    # Scrollable sidebar helpers
    # ------------------------------------------------------------------

    def _on_cards_frame_configure(self, _event: object) -> None:
        self._cards_canvas.configure(scrollregion=self._cards_canvas.bbox("all"))

    def _on_cards_canvas_configure(self, event: tk.Event) -> None:
        self._cards_canvas.itemconfigure(self._cards_window, width=event.width)

    def _add_session_to_sidebar(self, session: BrowserSession) -> SessionCard:
        """Create a SessionCard for *session* and pack it into the sidebar."""
        palette = _DARK_PALETTE if self._dark_mode else _LIGHT_PALETTE
        card = SessionCard(
            self._cards_inner,
            session,
            on_stop=self._on_card_stop,
            on_remove=self._on_card_remove,
        )
        card.apply_palette(palette)
        card.pack(fill=tk.X, padx=4, pady=(0, 6))
        self._session_cards[session.session_id] = card
        self._on_cards_frame_configure(None)
        return card

    def _on_card_stop(self, session: BrowserSession) -> None:
        """Called when a SessionCard's Stop button is clicked."""
        if session.is_active:
            session.close()
        card = self._session_cards.get(session.session_id)
        if card:
            card.refresh()
        if self.session is session:
            self._launch_status_var.set("No session running.")
            self._launch_start_btn.configure(state=tk.NORMAL)
            self._launch_stop_btn.configure(state=tk.DISABLED)
        self.append_log(f"Session stopped: {session.session_id}", "WARNING")

    def _on_card_remove(self, session: BrowserSession) -> None:
        """Called when a SessionCard's Remove button is clicked."""
        sid = session.session_id
        if session.is_active:
            session.close()
        card = self._session_cards.pop(sid, None)
        if card:
            card.destroy()
        self._sessions.pop(sid, None)
        if self.session is session:
            self.session = None
            self._launch_status_var.set("No session running.")
            self._launch_start_btn.configure(state=tk.NORMAL)
            self._launch_stop_btn.configure(state=tk.DISABLED)
        self.append_log(f"Session removed: {sid}", "INFO")

    # ------------------------------------------------------------------
    # Toolbar callbacks
    # ------------------------------------------------------------------

    def _on_navigate(self, _event: Optional[tk.Event] = None) -> None:
        """Route the URL bar contents through the active session guard."""
        url = sanitize_url(self._url_var.get())
        if not url:
            return
        if self.session is None or not self.session.is_active:
            self.append_log("No active session — navigation blocked.", "WARNING")
            return
        allowed = self.session.navigate(url)
        status = "allowed" if allowed else "blocked"
        level = "INFO" if allowed else "WARNING"
        self.append_log(f"[{self.session.session_id}] {status}: {url}", level)
        card = self._session_cards.get(self.session.session_id)
        if card:
            card.refresh()

    def _on_stop(self) -> None:
        """Stop the active session navigation (stub — real driver hook here)."""
        logger.info("Stop requested")
        self.append_log("Stop requested.", "DEBUG")

    def _on_refresh(self) -> None:
        """Refresh the current page (stub — real driver hook here)."""
        logger.info("Refresh requested")
        self.append_log("Refresh requested.", "DEBUG")

    # ------------------------------------------------------------------
    # Sidebar callbacks
    # ------------------------------------------------------------------

    def _on_new_session(self) -> None:
        """Switch to the Launcher tab to configure a new session."""
        self._notebook.select(0)
        logger.debug("Switched to Launcher tab")

    def _on_close_session(self) -> None:
        """Stop the session currently shown in the Launcher status."""
        self._on_launch_stop()

    # ------------------------------------------------------------------
    # Launcher tab callbacks
    # ------------------------------------------------------------------

    def _on_launch_start(self) -> None:
        """Create a real BrowserSession from Launcher tab settings and start it."""
        prefix = self._settings.get("session_prefix", "session")
        self._session_count += 1
        session_id = self._launch_id_var.get().strip() or f"{prefix}-{self._session_count}"

        rps = self._launch_rps_var.get()
        timeout = self._launch_timeout_var.get()

        # Optionally attach the selected proxy from the Proxies tab
        proxy: Optional[ProxyTunnel] = None
        if self._launch_use_proxy_var.get():
            sel = self._proxy_tree.selection()
            if sel:
                values = self._proxy_tree.item(sel[0], "values")
                if values:
                    host, port_str = str(values[0]).rsplit(":", 1)
                    proxy = ProxyTunnel(host, int(port_str))

        session = BrowserSession(
            session_id, proxy=proxy, rate_limit_rps=rps, nav_timeout=timeout
        )
        session.start()
        self._sessions[session_id] = session
        self.session = session
        self._add_session_to_sidebar(session)

        self._launch_status_var.set(f"Session '{session_id}' running.")
        self._launch_start_btn.configure(state=tk.DISABLED)
        self._launch_stop_btn.configure(state=tk.NORMAL)

        # Pre-fill next session ID
        self._launch_id_var.set(f"{prefix}-{self._session_count + 1}")

        self.append_log(
            f"Session started: {session_id} (rps={rps}, timeout={timeout}s)", "INFO"
        )

    def _on_launch_stop(self) -> None:
        """Stop the currently active session."""
        if self.session and self.session.is_active:
            sid = self.session.session_id
            self.session.close()
            card = self._session_cards.get(sid)
            if card:
                card.refresh()
            self.append_log(f"Session stopped: {sid}", "WARNING")
        self._launch_status_var.set("No session running.")
        self._launch_start_btn.configure(state=tk.NORMAL)
        self._launch_stop_btn.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Websites tab callbacks
    # ------------------------------------------------------------------

    def _on_allowed_add(self) -> None:
        domain = self._allowed_entry_var.get().strip()
        if domain:
            self._allowed_listbox.insert(tk.END, domain)
            self._allowed_entry_var.set("")
            self.append_log(f"Domain allowed: {domain}", "INFO")
            logger.info("Allowed domain added: %s", domain)

    def _on_allowed_remove(self) -> None:
        sel = self._allowed_listbox.curselection()
        if sel:
            domain = self._allowed_listbox.get(sel[0])
            self._allowed_listbox.delete(sel[0])
            self.append_log(f"Domain removed from allow-list: {domain}", "INFO")
            logger.info("Allowed domain removed: %s", domain)

    def _on_blocked_add(self) -> None:
        domain = self._blocked_entry_var.get().strip()
        if domain:
            BLOCKED_DOMAINS.add(domain.lower())
            self._blocked_listbox.insert(tk.END, domain)
            self._blocked_entry_var.set("")
            self.append_log(f"Domain blocked: {domain}", "WARNING")
            logger.info("Blocked domain added: %s", domain)

    def _on_blocked_remove(self) -> None:
        sel = self._blocked_listbox.curselection()
        if sel:
            domain = self._blocked_listbox.get(sel[0])
            BLOCKED_DOMAINS.discard(domain.lower())
            self._blocked_listbox.delete(sel[0])
            self.append_log(f"Domain unblocked: {domain}", "INFO")
            logger.info("Blocked domain removed: %s", domain)

    # ------------------------------------------------------------------
    # Proxies tab callbacks
    # ------------------------------------------------------------------

    def _on_proxy_add(self) -> None:
        host = self._proxy_host_var.get().strip()
        port = self._proxy_port_var.get()
        if host:
            self._proxy_tree.insert("", tk.END, values=(f"{host}:{port}", "idle", 0))
            self._proxy_host_var.set("")
            self.append_log(f"Proxy added: {host}:{port}", "INFO")
            logger.info("Proxy added: %s:%d", host, port)

    def _on_proxy_remove(self) -> None:
        sel = self._proxy_tree.selection()
        if sel:
            values = self._proxy_tree.item(sel[0], "values")
            self._proxy_tree.delete(sel[0])
            addr = values[0] if values else "?"
            self.append_log(f"Proxy removed: {addr}", "INFO")
            logger.info("Proxy removed: %s", addr)

    # ------------------------------------------------------------------
    # Log tab callbacks
    # ------------------------------------------------------------------

    def _on_log_clear(self) -> None:
        """Clear all text from the Log tab."""
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.configure(state=tk.DISABLED)
        logger.debug("Log cleared")

    def append_log(self, message: str, level: str = "INFO") -> None:
        """Append *message* to the Log tab text widget with level-based colouring."""
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.insert(tk.END, message + "\n", level.upper())
        self._log_text.configure(state=tk.DISABLED)
        if self._log_autoscroll_var.get():
            self._log_text.see(tk.END)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @classmethod
    def run(cls) -> None:
        """Create the Tk root, instantiate the app, and enter the event loop."""
        root = tk.Tk()
        cls(root)
        root.mainloop()
