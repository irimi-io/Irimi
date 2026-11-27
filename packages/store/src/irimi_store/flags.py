from collections.abc import Mapping

from irimi._wire import JsonValue
from irimi._wire.field_names import FLAGS


def read_flags(fields: Mapping[str, JsonValue]) -> tuple[JsonValue, ...]:
    flags = fields.get(FLAGS)
    return tuple(flags) if isinstance(flags, list) else ()
