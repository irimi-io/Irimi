from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

Item = TypeVar("Item")


@dataclass(frozen=True)
class Page(Generic[Item]):
    items: tuple[Item, ...]
    next_cursor: str | None


class InvalidCursorError(ValueError):
    pass


def count_items_that_fit(sizes: Sequence[int], limit: int, size_limit: int) -> int:
    count = 0
    total_size = 0
    for size in sizes[:limit]:
        total_size += size
        if count > 0 and total_size > size_limit:
            return count
        count += 1
    return count
