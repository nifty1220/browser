"""browser_guard: Utilities for controlling and validating browser access."""

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
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
