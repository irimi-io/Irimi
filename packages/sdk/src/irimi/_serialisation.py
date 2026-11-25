from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from irimi._wire import JsonValue, escape_reserved_key, mark_unserialisable


@dataclass(frozen=True)
class Serialised:
    value: JsonValue
    is_complete: bool


def serialise(value: object) -> Serialised:
    try:
        return _serialise(value)
    except RecursionError:
        return _record_by_type(value)


def serialise_arguments(
    arguments: Sequence[object], keyword_arguments: Mapping[str, object]
) -> Serialised:
    return serialise({"args": list(arguments), "kwargs": dict(keyword_arguments)})


def _serialise(value: object) -> Serialised:
    if value is None or isinstance(value, (bool, int, str)):
        return Serialised(value, is_complete=True)
    if isinstance(value, float):
        return (
            Serialised(value, is_complete=True) if math.isfinite(value) else _record_by_type(value)
        )
    if isinstance(value, (list, tuple)):
        return _serialise_items(value)
    if isinstance(value, dict) and all(isinstance(name, str) for name in value):
        return _serialise_object(value)
    return _record_by_type(value)


def _serialise_items(items: Sequence[object]) -> Serialised:
    serialised = [_serialise(item) for item in items]
    return Serialised(
        [item.value for item in serialised],
        is_complete=all(item.is_complete for item in serialised),
    )


def _serialise_object(items: Mapping[str, object]) -> Serialised:
    serialised = {name: _serialise(item) for name, item in items.items()}
    return Serialised(
        escape_reserved_key({name: item.value for name, item in serialised.items()}),
        is_complete=all(item.is_complete for item in serialised.values()),
    )


def _record_by_type(value: object) -> Serialised:
    value_type = type(value)
    return Serialised(
        mark_unserialisable(f"{value_type.__module__}.{value_type.__qualname__}"),
        is_complete=False,
    )
