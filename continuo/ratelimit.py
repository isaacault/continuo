# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""A tiny in-process, per-key sliding-window rate limiter (single uvicorn worker → no Redis)."""
import time
from collections import defaultdict, deque

from fastapi import Request

from . import config
from .errors import ApiError


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        cutoff = time.monotonic() - self.window
        q = self._hits[key]
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            self._hits.pop(key, None)
            q = self._hits[key]
        return q

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.max_events

    def register(self, key: str) -> None:
        self._prune(key).append(time.monotonic())

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def check(self, request: Request) -> None:
        """Register one event for the client; 429 once over the cap."""
        ip = client_ip(request)
        if self.is_blocked(ip):
            raise ApiError(429, "rate_limited", "Too many requests. Please wait a moment.", True)
        self.register(ip)


def client_ip(request: Request) -> str:
    """Best-effort client IP. Honours the left-most X-Forwarded-For entry only when TRUST_PROXY
    is set (otherwise the header is attacker-controlled)."""
    if config.settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


login_limiter = SlidingWindowLimiter(10, 300)
search_limiter = SlidingWindowLimiter(20, 10)
catalog_limiter = SlidingWindowLimiter(30, 10)


def configure() -> None:
    s = config.settings
    login_limiter.max_events = s.login_rate_limit_attempts
    login_limiter.window = s.login_rate_limit_window_seconds
