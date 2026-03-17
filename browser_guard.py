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

# Domains that are always blocked regardless of other rules
BLOCKED_DOMAINS: frozenset[str] = frozenset()

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


class BrowserGuardApp:
    """Top-level Tk application for BrowserGuard.

    Layout
    ------
    ┌──────────────────────────────────────────────┐
    │  Toolbar  (URL entry · Go · Stop · Refresh)  │
    ├───────────┬──────────────────────────────────┤
    │  Sidebar  │ [Launcher][Websites][Proxies][Log]│
    │  Sessions │  tab content                     │
    │  listbox  │                                  │
    │ [New][Cls]│                                  │
    └───────────┴──────────────────────────────────┘
    """

    def __init__(self, root: tk.Tk, session: Optional[BrowserSession] = None) -> None:
        self.root = root
        self.session = session

        self._setup_window()
        self._build_toolbar()
        self._build_main_area()

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
        self._refresh_btn.pack(side=tk.LEFT, padx=(0, 8), pady=4)

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
        """Create the sidebar panel on the left."""
        self.sidebar = ttk.Frame(self._pane, width=SIDEBAR_WIDTH)
        self.sidebar.pack_propagate(False)
        self._pane.add(self.sidebar, weight=0)

        # Section label
        ttk.Label(self.sidebar, text="Sessions", font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 2)
        )

        # Session list box
        self._session_listbox = tk.Listbox(self.sidebar, selectmode=tk.SINGLE)
        self._session_listbox.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self._session_listbox.bind("<<ListboxSelect>>", self._on_session_select)

        # Sidebar action buttons
        btn_frame = ttk.Frame(self.sidebar)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._new_session_btn = ttk.Button(
            btn_frame, text="New Session", command=self._on_new_session
        )
        self._new_session_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))

        self._close_session_btn = ttk.Button(
            btn_frame, text="Close", command=self._on_close_session
        )
        self._close_session_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)

        logger.debug("Sidebar built")

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
    # Toolbar callbacks (stubs)
    # ------------------------------------------------------------------

    def _on_navigate(self, _event: Optional[tk.Event] = None) -> None:
        """Called when the user presses Go or hits Enter in the URL bar."""
        url = sanitize_url(self._url_var.get())
        logger.info("Navigate requested: %s", url)

    def _on_stop(self) -> None:
        """Called when the user clicks Stop."""
        logger.info("Stop requested")

    def _on_refresh(self) -> None:
        """Called when the user clicks Refresh."""
        logger.info("Refresh requested")

    # ------------------------------------------------------------------
    # Sidebar callbacks (stubs)
    # ------------------------------------------------------------------

    def _on_session_select(self, _event: tk.Event) -> None:
        """Called when the user selects a session in the sidebar list."""
        selection = self._session_listbox.curselection()
        if selection:
            logger.debug("Session selected: index=%d", selection[0])

    def _on_new_session(self) -> None:
        """Called when the user clicks New Session."""
        logger.info("New session requested")

    def _on_close_session(self) -> None:
        """Called when the user clicks Close in the sidebar."""
        logger.info("Close session requested")

    # ------------------------------------------------------------------
    # Launcher tab callbacks (stubs)
    # ------------------------------------------------------------------

    def _on_launch_start(self) -> None:
        """Start a new BrowserSession with the options from the Launcher tab."""
        session_id = self._launch_id_var.get().strip()
        rps = self._launch_rps_var.get()
        timeout = self._launch_timeout_var.get()
        logger.info("Launch start: id=%r rps=%d timeout=%s", session_id, rps, timeout)
        self._launch_status_var.set(f"Session '{session_id}' running.")
        self._launch_start_btn.configure(state=tk.DISABLED)
        self._launch_stop_btn.configure(state=tk.NORMAL)

    def _on_launch_stop(self) -> None:
        """Stop the currently running session."""
        logger.info("Launch stop requested")
        self._launch_status_var.set("No session running.")
        self._launch_start_btn.configure(state=tk.NORMAL)
        self._launch_stop_btn.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Websites tab callbacks (stubs)
    # ------------------------------------------------------------------

    def _on_allowed_add(self) -> None:
        domain = self._allowed_entry_var.get().strip()
        if domain:
            self._allowed_listbox.insert(tk.END, domain)
            self._allowed_entry_var.set("")
            logger.info("Allowed domain added: %s", domain)

    def _on_allowed_remove(self) -> None:
        sel = self._allowed_listbox.curselection()
        if sel:
            domain = self._allowed_listbox.get(sel[0])
            self._allowed_listbox.delete(sel[0])
            logger.info("Allowed domain removed: %s", domain)

    def _on_blocked_add(self) -> None:
        domain = self._blocked_entry_var.get().strip()
        if domain:
            self._blocked_listbox.insert(tk.END, domain)
            self._blocked_entry_var.set("")
            logger.info("Blocked domain added: %s", domain)

    def _on_blocked_remove(self) -> None:
        sel = self._blocked_listbox.curselection()
        if sel:
            domain = self._blocked_listbox.get(sel[0])
            self._blocked_listbox.delete(sel[0])
            logger.info("Blocked domain removed: %s", domain)

    # ------------------------------------------------------------------
    # Proxies tab callbacks (stubs)
    # ------------------------------------------------------------------

    def _on_proxy_add(self) -> None:
        host = self._proxy_host_var.get().strip()
        port = self._proxy_port_var.get()
        if host:
            self._proxy_tree.insert("", tk.END, values=(f"{host}:{port}", "idle", 0))
            self._proxy_host_var.set("")
            logger.info("Proxy added: %s:%d", host, port)

    def _on_proxy_remove(self) -> None:
        sel = self._proxy_tree.selection()
        if sel:
            values = self._proxy_tree.item(sel[0], "values")
            self._proxy_tree.delete(sel[0])
            logger.info("Proxy removed: %s", values[0] if values else "?")

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
        """Append *message* to the Log tab text widget.

        *level* should be one of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``.
        The text is colour-tagged accordingly.
        """
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
