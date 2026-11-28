from __future__ import annotations

import io


class AtomicTotal:
    def __init__(self) -> None:
        self._cursor = io.BytesIO()

    def add(self, amount: int) -> int:
        return self._cursor.seek(amount, io.SEEK_CUR)

    def read(self) -> int:
        return self._cursor.tell()
