from __future__ import annotations

import dataclasses


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
