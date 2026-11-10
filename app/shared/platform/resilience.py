"""Bound repeated dependency failures without treating a fallback as a successful model call."""

from threading import Lock
from time import monotonic

from app.shared.errors import AppError


class CircuitBreaker:
    def __init__(self, threshold=5, cooldown=30.0):
        self.threshold, self.cooldown = threshold, cooldown
        self.failures, self.blocked_until = 0, 0.0
        self.probing = False
        self.lock = Lock()

    async def enter(self):
        with self.lock:
            if self.failures >= self.threshold:
                if monotonic() < self.blocked_until or self.probing:
                    raise AppError("DEPENDENCY_CIRCUIT_OPEN", "dependency recovery pending", 503)
                self.probing = True

    async def finish(self, failed):
        with self.lock:
            self.probing = False
            if failed:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.blocked_until = monotonic() + self.cooldown
            else:
                self.failures, self.blocked_until = 0, 0.0

    async def abort(self):
        with self.lock:
            self.probing = False


_circuits: dict[str, CircuitBreaker] = {}
_registry_lock = Lock()


def circuit_for(dependency):
    with _registry_lock:
        return _circuits.setdefault(dependency, CircuitBreaker())
