"""Simple in-memory rate limiter for login/register."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, *, max_hits: int, window_sec: float) -> None:
        self.max_hits = max(1, int(max_hits))
        self.window_sec = float(window_sec)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_sec)."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            cutoff = now - self.window_sec
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_hits:
                retry = max(0.0, self.window_sec - (now - q[0]))
                return False, retry
            q.append(now)
            return True, 0.0


# 10 attempts / 5 minutes per IP+username bucket; also IP-only 30/5min
login_user_limiter = SlidingWindowLimiter(max_hits=10, window_sec=300)
login_ip_limiter = SlidingWindowLimiter(max_hits=30, window_sec=300)
register_ip_limiter = SlidingWindowLimiter(max_hits=5, window_sec=600)
