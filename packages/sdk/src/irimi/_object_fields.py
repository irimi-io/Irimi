from __future__ import annotations

import dataclasses
from types import BuiltinFunctionType

HEAP_TYPE_FLAG = 1 << 9


def read_fields(value: object) -> dict[str, object] | None:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    model_type = type(value)
    model_fields = getattr(model_type, "model_fields", None)
    if not callable(getattr(model_type, "model_dump", None)) or not isinstance(model_fields, dict):
        return None
    fields = {name: getattr(value, name) for name in model_fields}
    extra_fields = getattr(value, "model_extra", None)
    return {**fields, **extra_fields} if isinstance(extra_fields, dict) else fields


def keeps_state_outside_attributes(object_type: type) -> bool:
    for base in object_type.__mro__:
        if base is not object and _lays_out_its_own_instances(base):
            return True
    return False


def _lays_out_its_own_instances(base: type) -> bool:
    if not base.__flags__ & HEAP_TYPE_FLAG:
        return True
    constructor = base.__new__
    return isinstance(constructor, BuiltinFunctionType) and constructor.__self__ is base
