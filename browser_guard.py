"""browser_guard: Utilities for controlling and validating browser access."""

import logging
import re
import time
from typing import Optional
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
