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
            self._evict_expired(cutoff)
            return True, 0.0

    def _evict_expired(self, cutoff: float) -> None:
        """Drop buckets that fully aged out.

        Without this the dict only ever grew: every distinct key seen (one per
        attacker-chosen username, for instance) kept an empty deque forever.
        Amortized — only runs a full sweep once the map gets large.
        """
        if len(self._hits) < 1024:
            return
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] < cutoff]
        for k in stale:
            self._hits.pop(k, None)


# 10 attempts / 5 minutes per IP+username bucket; also IP-only 30/5min
login_user_limiter = SlidingWindowLimiter(max_hits=10, window_sec=300)
login_ip_limiter = SlidingWindowLimiter(max_hits=30, window_sec=300)
register_ip_limiter = SlidingWindowLimiter(max_hits=5, window_sec=600)

# 容量雷达:每 (user, tenant) 6 次 / 5 分钟。
#
# 依据:热态一次探测 = 每个可用域恰好一个 Oracle 请求(≤3,免费用户最常见的
# ap-tokyo-1 / ap-singapore-1 等单 AD 区域就是 1 个),6 次/5 分钟 ≈ 3.6 请求/分钟,
# 远低于本仓已有的 WebSSH 先例(20 次握手/分钟 × 约 3 次 OCI 调用)。功能上 6 次
# 足够覆盖「开页 + 改一次规格 + 复查几次」。
#
# 只按 (user, tenant) 一道,不再加一道跨租户的:限流保护的是 Oracle 的
# **per-tenancy** 桶,一个用户有 20 个租户时那是 20 个互不相干的桶,跨租户加总
# 没有物理意义。
#
# 键里**绝不含规格**:规格是客户端可控的,把它放进键里等于把限流交给对方绕开
# (memory_in_gbs 加 1 就是一个新桶)。缓存键含规格是对的(答案确实随规格变),
# 限流键含规格是错的 —— 两者的职责不同。
#
# 注意这个限流器是**进程内**的(web/AUDIT.md 里已接受的缺口),真实上限是
# OCIBOT_API_WORKERS × max_hits:默认 2 个 worker 即 12 次/5 分钟。把
# OCIBOT_API_WORKERS 调到 8 会同时把这个 OCI 花销上限抬到 4 倍。真正的主防线是
# capacity_radar 里那层 60 秒进程内缓存,它对连点天然免疫。
capacity_report_limiter = SlidingWindowLimiter(max_hits=6, window_sec=300)
