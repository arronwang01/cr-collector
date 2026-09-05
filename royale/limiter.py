"""Self-tuning request rate.

RoyaleAPI's 429 threshold is not published and moves, so nothing here guesses
it: the limiter probes upward while requests succeed and halves on every 429,
which settles just under whatever the real ceiling is right now. Plain AIMD --
the same control loop TCP uses, for the same reason.

One instance is shared by every worker thread; `acquire` is the only blocking
call and it hands out evenly spaced slots, so 12 threads at 8 req/s behave like
8 req/s and not like 12 bursts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Limiter:
    rate: float = 2.0        # requests per second, right now
    floor: float = 0.5       # never crawl slower than this
    ceiling: float = 8.0     # never probe past this -- tunable, kept low by default
    step: float = 0.25       # additive increase per successful round, after a 429
    growth: float = 1.03     # multiplicative increase per success, before any 429
    penalty: float = 2.0     # minimum pause after a 429

    hits: int = 0            # 429s seen
    peak: float = 0.0        # fastest rate that held
    sent: int = 0

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next: float = field(default=0.0, repr=False)

    def acquire(self) -> None:
        """Block until this thread's slot in the schedule comes up."""
        with self._lock:
            now = time.monotonic()
            self._next = max(now, self._next) + 1.0 / self.rate
            wait = self._next - now
            self.sent += 1
        if wait > 0:
            time.sleep(wait)

    def ok(self) -> None:
        """A request came back clean: go faster."""
        with self._lock:
            if self.hits:
                # Congestion avoidance: step/rate, not step, so one full
                # increment lands per round of requests instead of per request.
                self.rate = min(self.ceiling, self.rate + self.step / self.rate)
            else:
                # Slow start. No 429 has ever been seen, so the ceiling is still
                # unknown and creeping up would waste the whole run finding it.
                self.rate = min(self.ceiling, self.rate * self.growth)
            self.peak = max(self.peak, self.rate)

    def blocked(self, retry_after: float = 0.0) -> None:
        """A 429: halve the rate and hold everyone off."""
        with self._lock:
            self.hits += 1
            self.rate = max(self.floor, self.rate / 2)
            self._next = time.monotonic() + max(retry_after, self.penalty)

    def __str__(self) -> str:
        return f"{self.rate:.1f}/s peak {self.peak:.1f}/s · {self.hits} x 429"
