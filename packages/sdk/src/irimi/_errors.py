from __future__ import annotations

from irimi._recording import Payload
from irimi._serialisation import name_type, serialise
from irimi._wire import JsonValue, field_names


def describe_error(error: BaseException) -> Payload:
    message = serialise(str(error))
    fields: dict[str, JsonValue] = {
        field_names.STATUS: "error",
        field_names.ERROR: {"type": name_type(type(error)), "message": message.value},
    }
    return Payload(fields, message.approximate_size)
