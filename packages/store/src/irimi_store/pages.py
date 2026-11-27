from dataclasses import dataclass
from typing import Generic, TypeVar

Item = TypeVar("Item")


@dataclass(frozen=True)
class Page(Generic[Item]):
    items: tuple[Item, ...]
    next_cursor: str | None


class InvalidCursorError(ValueError):
    pass
