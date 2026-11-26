from __future__ import annotations

import logging
import threading

logger = logging.getLogger("irimi")


class DropTally:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._count_already_taken = 0

    def record(self, count: int, warning: str, has_exception: bool = False) -> None:
        with self._lock:
            is_first_drop = self._count == 0
            self._count += count
        if is_first_drop:
            logger.warning(warning, exc_info=has_exception)

    def take_new_count(self) -> int:
        with self._lock:
            new_count = self._count - self._count_already_taken
            self._count_already_taken = self._count
            return new_count
