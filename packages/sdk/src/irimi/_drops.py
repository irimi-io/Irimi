from __future__ import annotations

import logging

from irimi._atomic_total import AtomicTotal

logger = logging.getLogger("irimi")


class DropTally:
    def __init__(self) -> None:
        self._count = AtomicTotal()
        self._count_already_taken = 0

    def record(self, count: int, warning: str, has_exception: bool = False) -> None:
        is_first_drop = self._count.add(count) == count
        if is_first_drop:
            logger.warning(warning, exc_info=has_exception)

    def take_new_count(self) -> int:
        count = self._count.read()
        new_count = count - self._count_already_taken
        self._count_already_taken = count
        return new_count
