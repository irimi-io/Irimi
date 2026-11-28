from __future__ import annotations

import itertools
import logging
import queue

logger = logging.getLogger("irimi")


class DropTally:
    def __init__(self) -> None:
        self._records_made = itertools.count()
        self._counts_not_taken: queue.SimpleQueue[int] = queue.SimpleQueue()

    def record(self, count: int, warning: str, has_exception: bool = False) -> None:
        self._counts_not_taken.put(count)
        is_first_drop = next(self._records_made) == 0
        if is_first_drop:
            logger.warning(warning, exc_info=has_exception)

    def take_new_count(self) -> int:
        new_count = 0
        while True:
            try:
                new_count += self._counts_not_taken.get_nowait()
            except queue.Empty:
                return new_count
