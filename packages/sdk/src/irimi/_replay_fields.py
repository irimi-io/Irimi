from __future__ import annotations

from irimi._replay_decisions import Decision
from irimi._wire import JsonValue, field_names


def describe_decision(decision: Decision) -> dict[str, JsonValue]:
    fields: dict[str, JsonValue] = {field_names.ANSWERED: decision.answer}
    if decision.matched is not None:
        fields[field_names.MATCHED] = {
            field_names.SPAN_ID: decision.matched.span_id,
            field_names.RULE: decision.rule,
        }
    if decision.is_changed:
        fields[field_names.CHANGED] = True
    if decision.flags:
        fields[field_names.FLAGS] = list(decision.flags)
    return fields
