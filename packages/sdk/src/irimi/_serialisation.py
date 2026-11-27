from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from irimi._wire import JsonValue, escape_reserved_key, mark_unserialisable

SIZE_OF_A_SMALL_VALUE = 8


@dataclass(frozen=True)
class Serialised:
    value: JsonValue
    is_complete: bool
    approximate_size: int


def serialise(value: object) -> Serialised:
    snapshot = _Snapshot()
    try:
        taken = snapshot.take(value)
    except RecursionError:
        return Serialised(
            mark_unserialisable(name_type(type(value))), is_complete=False, approximate_size=0
        )
    return Serialised(taken, snapshot.is_complete, snapshot.approximate_size)


def serialise_arguments(
    arguments: Sequence[object], keyword_arguments: Mapping[str, object]
) -> Serialised:
    return serialise({"args": list(arguments), "kwargs": dict(keyword_arguments)})


def name_type(named: type) -> str:
    return f"{named.__module__}.{named.__qualname__}"


def _is_whole_text(text: str) -> bool:
    if text.isascii():
        return True
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


class _Snapshot:
    def __init__(self) -> None:
        self.is_complete = True
        self.approximate_size = 0

    def take(self, value: object) -> JsonValue:
        if isinstance(value, str):
            if not _is_whole_text(value):
                return self._record_by_type(value)
            self.approximate_size += len(value)
            return value
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else self._record_by_type(value)
        if isinstance(value, (list, tuple)):
            self.approximate_size += SIZE_OF_A_SMALL_VALUE * len(value)
            return [self.take(item) for item in value]
        if isinstance(value, dict) and all(
            isinstance(name, str) and _is_whole_text(name) for name in value
        ):
            return self._take_object(value)
        return self._record_by_type(value)

    def _take_object(self, items: dict[str, object]) -> JsonValue:
        self.approximate_size += sum(map(len, items)) + SIZE_OF_A_SMALL_VALUE * len(items)
        return escape_reserved_key({name: self.take(item) for name, item in items.items()})

    def _record_by_type(self, value: object) -> JsonValue:
        self.is_complete = False
        return mark_unserialisable(name_type(type(value)))
