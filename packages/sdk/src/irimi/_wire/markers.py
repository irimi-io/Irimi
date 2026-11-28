from __future__ import annotations

from irimi._wire.json_value import JsonValue

RESERVED_KEY = "$irimi"
LITERAL = "literal"


def escape_reserved_keys(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [escape_reserved_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    return escape_reserved_key({name: escape_reserved_keys(item) for name, item in value.items()})


def escape_reserved_key(items: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if RESERVED_KEY in items:
        return {RESERVED_KEY: LITERAL, "value": items}
    return items


def mark_unserialisable(type_name: str) -> dict[str, JsonValue]:
    return {RESERVED_KEY: "unserialisable", "type": type_name}


def mark_redacted(reason: str) -> dict[str, JsonValue]:
    return {RESERVED_KEY: "redacted", "reason": reason}


def unescape_reserved_keys(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [unescape_reserved_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    if RESERVED_KEY not in value:
        return _unescape_items(value)
    literal = value.get("value")
    if value[RESERVED_KEY] == LITERAL and isinstance(literal, dict):
        return _unescape_items(literal)
    return value


def _unescape_items(items: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {name: unescape_reserved_keys(item) for name, item in items.items()}
