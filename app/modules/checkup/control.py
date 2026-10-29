"""Cooperative pause for the existing single-worker checkup process."""
import threading
import time


class CheckupInterrupted(BaseException):
    """The server is closing; retain the last checkpoint instead of resuming IO."""


class CheckupCancelled(CheckupInterrupted):
    """User cancellation must bypass broad provider error/retry recovery."""


class CheckupControl:
    def __init__(self):
        self._condition = threading.Condition()
        self.paused = False
        self._paused_at = None
        self._paused_seconds = 0.0
        self._stopped = False
        self.cancelled = False

    def pause(self):
        with self._condition:
            if not self.paused:
                self.paused = True
                self._paused_at = time.monotonic()

    def resume(self):
        with self._condition:
            if self.paused:
                self._paused_seconds += time.monotonic() - self._paused_at
                self._paused_at = None
                self.paused = False
                self._condition.notify_all()

    def checkpoint(self):
        with self._condition:
            while self.paused and not self._stopped and not self.cancelled:
                self._condition.wait()
            if self.cancelled:
                raise CheckupCancelled()
            if self._stopped:
                raise CheckupInterrupted()

    def cancel(self):
        with self._condition:
            self.cancelled = True
            self._condition.notify_all()

    def stop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def active_time(self):
        """Monotonic clock with all paused intervals removed from task budgets."""
        with self._condition:
            now = time.monotonic()
            paused = self._paused_seconds + (now - self._paused_at if self.paused else 0)
            return now - paused
