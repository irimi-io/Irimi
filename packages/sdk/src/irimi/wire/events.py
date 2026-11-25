from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from irimi.wire.json_value import JsonValue

EVENT_FIELD_NAMES = frozenset({"id", "type", "trace_id", "span_id", "time_unix_nano"})
START_FIELD_NAMES = frozenset({"parent_span_id", "kind", "name", "seq"})


class WireFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    trace_id: str
    span_id: str
    time_unix_nano: int
    fields: Mapping[str, JsonValue]


@dataclass(frozen=True)
class StartEvent(Event):
    parent_span_id: str | None
    kind: str
    name: str
    seq: int


@dataclass(frozen=True)
class EndEvent(Event):
    pass


@dataclass(frozen=True)
class Batch:
    events: tuple[Event, ...]
    fields: Mapping[str, JsonValue]


def parse_batch(document: JsonValue) -> Batch:
    if not isinstance(document, dict):
        raise WireFormatError("batch: expected an object")
    events = document.get("events")
    if not isinstance(events, list):
        raise WireFormatError("batch.events: expected a list")
    return Batch(
        events=tuple(_parse_event(event, f"events[{index}]") for index, event in enumerate(events)),
        fields=_without(document, frozenset({"events"})),
    )


def write_batch(batch: Batch) -> dict[str, JsonValue]:
    events: list[JsonValue] = [_write_event(event) for event in batch.events]
    return {**batch.fields, "events": events}


def _parse_event(document: JsonValue, place: str) -> Event:
    if not isinstance(document, dict):
        raise WireFormatError(f"{place}: expected an object")
    event_type = _read_string(document, "type", place)
    if event_type == "start":
        return _parse_start_event(document, place)
    event_class = EndEvent if event_type == "end" else Event
    return event_class(
        id=_read_string(document, "id", place),
        type=event_type,
        trace_id=_read_string(document, "trace_id", place),
        span_id=_read_string(document, "span_id", place),
        time_unix_nano=_read_time(document, place),
        fields=_without(document, EVENT_FIELD_NAMES),
    )


def _parse_start_event(document: dict[str, JsonValue], place: str) -> StartEvent:
    return StartEvent(
        id=_read_string(document, "id", place),
        type="start",
        trace_id=_read_string(document, "trace_id", place),
        span_id=_read_string(document, "span_id", place),
        time_unix_nano=_read_time(document, place),
        parent_span_id=_read_parent_span_id(document, place),
        kind=_read_string(document, "kind", place),
        name=_read_string(document, "name", place),
        seq=_read_seq(document, place),
        fields=_without(document, EVENT_FIELD_NAMES | START_FIELD_NAMES),
    )


def _write_event(event: Event) -> dict[str, JsonValue]:
    document: dict[str, JsonValue] = {
        **event.fields,
        "id": event.id,
        "type": event.type,
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "time_unix_nano": str(event.time_unix_nano),
    }
    if isinstance(event, StartEvent):
        document.update(
            parent_span_id=event.parent_span_id, kind=event.kind, name=event.name, seq=event.seq
        )
    return document


def _read_string(document: dict[str, JsonValue], name: str, place: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise WireFormatError(f"{place}.{name}: expected a non-empty string")
    return value


def _read_time(document: dict[str, JsonValue], place: str) -> int:
    value = document.get("time_unix_nano")
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()):
        raise WireFormatError(f"{place}.time_unix_nano: expected a string of digits")
    return int(value)


def _read_parent_span_id(document: dict[str, JsonValue], place: str) -> str | None:
    if "parent_span_id" not in document:
        raise WireFormatError(f"{place}.parent_span_id: expected a string or null")
    if document["parent_span_id"] is None:
        return None
    return _read_string(document, "parent_span_id", place)


def _read_seq(document: dict[str, JsonValue], place: str) -> int:
    value = document.get("seq")
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireFormatError(f"{place}.seq: expected a whole number")
    return value


def _without(document: dict[str, JsonValue], names: frozenset[str]) -> dict[str, JsonValue]:
    return {name: value for name, value in document.items() if name not in names}
