"""登录失败限流。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class LoginRateLimiter:
    """按客户端地址限制短时间内的连续登录失败。"""

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 300,
        max_clients: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self.clock = clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, client_key: str, now: float) -> None:
        attempts = self._attempts.get(client_key)
        if attempts is None:
            return
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def _cleanup_expired(self, now: float) -> None:
        for client_key in list(self._attempts):
            self._prune(client_key, now)
            if not self._attempts[client_key]:
                self._attempts.pop(client_key, None)

    def is_blocked(self, client_key: str) -> bool:
        with self._lock:
            now = self.clock()
            self._cleanup_expired(now)
            return len(self._attempts.get(client_key, ())) >= self.max_attempts

    def record_failure(self, client_key: str) -> None:
        with self._lock:
            now = self.clock()
            self._cleanup_expired(now)
            if client_key not in self._attempts and len(self._attempts) >= self.max_clients:
                oldest_key = min(self._attempts, key=lambda key: self._attempts[key][-1])
                self._attempts.pop(oldest_key, None)
            self._attempts.setdefault(client_key, deque()).append(now)

    def clear(self, client_key: str) -> None:
        with self._lock:
            self._attempts.pop(client_key, None)
