"""Request pacing shared across processes that use the same credentials.

Reddit's rate limit is per account. Two processes each pacing themselves at one request per second
still hit Reddit at two per second, and one of them eats a string of 429s with 50-60 s enforced
sleeps. ``FileLockPacer`` fixes that with a tiny state file per token holding ``next_allowed_at``,
guarded by ``fcntl.flock`` (POSIX only). Before each request a process locks the file, sleeps until
allowed, advances the timestamp by the interval, and releases; the HTTP call happens outside the
lock so one process's network latency never blocks another's pacing check. On a 429 the penalized
process writes ``now + X-Ratelimit-Reset`` so its siblings back off too.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Protocol

DEFAULT_STATE_DIR = Path.home() / ".cache" / "reddit-rss-miner"


class Pacer(Protocol):
    def acquire(self) -> None: ...
    def penalize(self, seconds: float) -> None: ...


class NoPacer:
    def acquire(self) -> None: ...
    def penalize(self, seconds: float) -> None: ...


class IntervalPacer:
    """Single-process pacing: at most one request per ``interval`` seconds."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval, self._clock, self._sleep = interval, clock, sleep
        self._next_allowed = 0.0

    def acquire(self) -> None:
        now = self._clock()
        if now < self._next_allowed:
            self._sleep(self._next_allowed - now)
            now = self._next_allowed
        self._next_allowed = now + self._interval

    def penalize(self, seconds: float) -> None:
        self._next_allowed = max(self._next_allowed, self._clock() + seconds)


def state_path_for(token: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    """One state file per credential, named by a hash so the token itself never lands on disk."""
    return state_dir / f"{hashlib.sha1(token.encode()).hexdigest()[:12]}.pace"


class FileLockPacer:
    """Cross-process pacing via a flock-guarded timestamp file. See module docstring."""

    def __init__(self, path: Path, interval: float, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._path, self._interval, self._clock, self._sleep = Path(path), interval, clock, sleep
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _locked(self, update: Callable[[float, float], float]) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 64)
            next_allowed = float(raw) if raw.strip() else 0.0
            new_value = update(self._clock(), next_allowed)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{new_value:.6f}".encode())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def acquire(self) -> None:
        def wait_then_reserve(now: float, next_allowed: float) -> float:
            if now < next_allowed:
                self._sleep(next_allowed - now)
                now = next_allowed
            return now + self._interval
        self._locked(wait_then_reserve)

    def penalize(self, seconds: float) -> None:
        self._locked(lambda now, next_allowed: max(next_allowed, now + seconds))
