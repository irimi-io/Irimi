from __future__ import annotations

from irimi._wire.json_value import JsonValue

RESERVED_KEY = "$irimi"
LITERAL = "literal"


def escape_reserved_keys(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [escape_reserved_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    escaped = {name: escape_reserved_keys(item) for name, item in value.items()}
    if RESERVED_KEY in value:
        return {RESERVED_KEY: LITERAL, "value": escaped}
    return escaped


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
