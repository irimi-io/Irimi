import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from irimi._wire import JsonValue


@dataclass(frozen=True)
class StoredFields:
    core: tuple[str | None, ...]
    data: str


def split_fields(fields: Mapping[str, JsonValue], core_field_names: Sequence[str]) -> StoredFields:
    return StoredFields(
        core=tuple(
            json.dumps(fields[name]) if name in fields else None for name in core_field_names
        ),
        data=json.dumps(
            {name: value for name, value in fields.items() if name not in core_field_names}
        ),
    )


def join_fields(stored: StoredFields, core_field_names: Sequence[str]) -> dict[str, JsonValue]:
    fields: dict[str, JsonValue] = json.loads(stored.data)
    for name, text in zip(core_field_names, stored.core, strict=True):
        if text is not None:
            fields[name] = json.loads(text)
    return fields
