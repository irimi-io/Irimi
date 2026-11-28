from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from irimi._object_fields import read_fields
from irimi._wire import JsonValue, escape_reserved_key, mark_object, mark_unserialisable

SIZE_OF_A_SMALL_VALUE = 8


@dataclass(frozen=True)
class Serialised:
    value: JsonValue
    is_complete: bool
    approximate_size: int
    unread_objects: int


def serialise(value: object) -> Serialised:
    snapshot = _Snapshot()
    try:
        taken = snapshot.take(value)
    except RecursionError:
        return Serialised(
            mark_unserialisable(name_type(type(value))),
            is_complete=False,
            approximate_size=0,
            unread_objects=0,
        )
    return Serialised(
        taken, snapshot.is_complete, snapshot.approximate_size, snapshot.unread_objects
    )


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


def _has_whole_names(items: dict[str, object]) -> bool:
    return all(isinstance(name, str) and _is_whole_text(name) for name in items)


class _Snapshot:
    def __init__(self) -> None:
        self.is_complete = True
        self.approximate_size = 0
        self.unread_objects = 0

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
        if isinstance(value, dict) and _has_whole_names(value):
            return self._take_object(value)
        return self._take_fields(value)

    def _take_fields(self, value: object) -> JsonValue:
        try:
            fields = read_fields(value)
        except RecursionError:
            raise
        except Exception:
            self.unread_objects += 1
            return self._record_by_type(value)
        if fields is None or not _has_whole_names(fields):
            return self._record_by_type(value)
        type_name = name_type(type(value))
        self.approximate_size += len(type_name) + SIZE_OF_A_SMALL_VALUE
        return mark_object(type_name, self._take_object(fields))

    def _take_object(self, items: dict[str, object]) -> JsonValue:
        self.approximate_size += sum(map(len, items)) + SIZE_OF_A_SMALL_VALUE * len(items)
        return escape_reserved_key({name: self.take(item) for name, item in items.items()})

    def _record_by_type(self, value: object) -> JsonValue:
        self.is_complete = False
        return mark_unserialisable(name_type(type(value)))
