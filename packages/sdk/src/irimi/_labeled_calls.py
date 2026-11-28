from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from irimi._recording import Payload
from irimi._serialisation import serialise, serialise_arguments
from irimi._wire import JsonValue, field_names, write_flags


@dataclass(frozen=True)
class CallLabel:
    kind: Literal["workflow", "function"]
    name: str
    import_path: str
    key: str | None = None
    is_write: bool = False
    is_read: bool = False


@dataclass(frozen=True)
class RunSettings:
    environment: str | None
    version: str | None
    commit: str | None


def describe_start(
    label: CallLabel,
    arguments: Sequence[object],
    keyword_arguments: Mapping[str, object],
    settings: RunSettings,
    is_root: bool,
) -> Payload:
    call_input = serialise_arguments(arguments, keyword_arguments)
    fields: dict[str, JsonValue] = {field_names.INPUT: call_input.value}
    if label.is_write:
        fields[field_names.WRITE] = True
    if label.is_read:
        fields[field_names.READ] = True
    if is_root:
        fields.update(_describe_root(label, settings, call_input.is_complete))
    return Payload(fields, call_input.approximate_size)


def describe_output(output: object) -> Payload:
    serialised = serialise(output)
    fields: dict[str, JsonValue] = {field_names.STATUS: "ok", field_names.OUTPUT: serialised.value}
    if not serialised.is_complete:
        fields[field_names.FLAGS] = write_flags("unserialisable_output")
    return Payload(fields, serialised.approximate_size)


def _describe_root(
    label: CallLabel, settings: RunSettings, is_input_complete: bool
) -> dict[str, JsonValue]:
    if label.kind == "function":
        return {field_names.FLAGS: write_flags("unattached")}
    deployment: dict[str, JsonValue] = {
        name: value
        for name, value in (
            ("version", settings.version or settings.commit),
            ("commit", settings.commit),
        )
        if value is not None
    }
    fields: dict[str, JsonValue] = {
        field_names.MODE: "normal",
        field_names.STARTED_BY: {"kind": "decorated_function", "import_path": label.import_path},
        field_names.DEPLOYMENT: deployment,
    }
    if label.key is not None:
        fields[field_names.KEY] = label.key
    if settings.environment is not None:
        fields[field_names.ENVIRONMENT] = settings.environment
    if not is_input_complete:
        fields[field_names.FLAGS] = write_flags("not_replayable")
    return fields
