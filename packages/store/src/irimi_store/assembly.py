from collections.abc import Mapping
from typing import Literal

from irimi._wire import EndEvent, JsonValue, StartEvent
from irimi._wire.field_names import ENVIRONMENT, EVENTS_PRODUCED, MODE, STATUS

from irimi_store.flags import read_flags
from irimi_store.reader_documents import RunSummary
from irimi_store.storage import StoredRoot

Condition = Literal["unfinished", "incomplete", "not_replayable"]
NOT_REPLAYABLE = "not_replayable"


def read_conditions(
    start: StartEvent | None, end: EndEvent | None, events_held: int
) -> tuple[Condition, ...]:
    if start is None:
        return ()
    conditions: list[Condition] = []
    if end is None:
        conditions.append("unfinished")
    if end is not None and _holds_fewer_than_produced(end, events_held):
        conditions.append("incomplete")
    if NOT_REPLAYABLE in read_flags(start.fields):
        conditions.append("not_replayable")
    return tuple(conditions)


def summarise_root(root: StoredRoot) -> RunSummary:
    end_fields: Mapping[str, JsonValue] = {} if root.end is None else root.end.fields
    return RunSummary(
        id=root.start.trace_id,
        span_id=root.start.span_id,
        kind=root.start.kind,
        name=root.start.name,
        start_time_unix_nano=root.start.time_unix_nano,
        end_time_unix_nano=None if root.end is None else root.end.time_unix_nano,
        status=end_fields.get(STATUS),
        flags=merge_flags(root.start.fields, end_fields),
        environment=root.start.fields.get(ENVIRONMENT),
        mode=root.start.fields.get(MODE),
        conditions=read_conditions(root.start, root.end, root.events_held),
        fields={},
    )


def merge_flags(
    start_fields: Mapping[str, JsonValue], end_fields: Mapping[str, JsonValue]
) -> tuple[JsonValue, ...]:
    start_flags = read_flags(start_fields)
    return start_flags + tuple(flag for flag in read_flags(end_fields) if flag not in start_flags)


def _holds_fewer_than_produced(end: EndEvent, events_held: int) -> bool:
    events_produced = end.fields.get(EVENTS_PRODUCED)
    if isinstance(events_produced, bool) or not isinstance(events_produced, int):
        return False
    return events_held < events_produced
