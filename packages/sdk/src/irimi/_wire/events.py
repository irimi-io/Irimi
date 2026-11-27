from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass

from irimi._wire.json_value import JsonValue

WIRE_VERSION = "v1"
EVENTS_PATH = f"/{WIRE_VERSION}/events"
RUN_EVENTS_PATH = f"/{WIRE_VERSION}/runs/{{trace_id}}/events"
EVENT_FIELD_NAMES = frozenset({"id", "type", "trace_id", "span_id", "time_unix_nano"})
START_FIELD_NAMES = frozenset({"parent_span_id", "kind", "name", "seq"})
ID_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-_")
LARGEST_ID_LENGTH = 128
LARGEST_WHOLE_NUMBER = 2**63 - 1
LARGEST_SEQ = 2**53 - 1
DIGITS_IN_LARGEST_WHOLE_NUMBER = len(str(LARGEST_WHOLE_NUMBER))


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


@dataclass(frozen=True)
class RefusedEvent:
    index: int
    reason: str


@dataclass(frozen=True)
class ParsedBatch:
    batch: Batch
    refused: tuple[RefusedEvent, ...]


def parse_batch(document: JsonValue) -> ParsedBatch:
    if not isinstance(document, dict):
        raise WireFormatError("batch: expected an object")
    events = document.get("events")
    if not isinstance(events, list):
        raise WireFormatError("batch.events: expected a list")
    accepted: list[Event] = []
    refused: list[RefusedEvent] = []
    for index, event in enumerate(events):
        try:
            accepted.append(parse_event(event, f"events[{index}]"))
        except WireFormatError as error:
            refused.append(RefusedEvent(index=index, reason=str(error)))
    return ParsedBatch(
        batch=Batch(events=tuple(accepted), fields=_without(document, frozenset({"events"}))),
        refused=tuple(refused),
    )


def write_batch(batch: Batch) -> dict[str, JsonValue]:
    events: list[JsonValue] = [write_event(event) for event in batch.events]
    return {**batch.fields, "events": events}


def parse_refused_events(document: JsonValue) -> tuple[RefusedEvent, ...]:
    if not isinstance(document, dict):
        raise WireFormatError("answer: expected an object")
    refused = document.get("refused")
    if not isinstance(refused, list):
        raise WireFormatError("answer.refused: expected a list")
    return tuple(
        _parse_refused_event(item, f"refused[{place}]") for place, item in enumerate(refused)
    )


def write_refused_events(refused: tuple[RefusedEvent, ...]) -> dict[str, JsonValue]:
    return {"refused": [{"index": event.index, "reason": event.reason} for event in refused]}


def _parse_refused_event(document: JsonValue, place: str) -> RefusedEvent:
    if not isinstance(document, dict):
        raise WireFormatError(f"{place}: expected an object")
    index = document.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise WireFormatError(f"{place}.index: expected a whole number")
    return RefusedEvent(index=index, reason=_read_string(document, "reason", place))


def parse_event(document: JsonValue, place: str = "event") -> Event:
    if not isinstance(document, dict):
        raise WireFormatError(f"{place}: expected an object")
    event_type = _read_string(document, "type", place)
    if event_type == "start":
        return _parse_start_event(document, place)
    event_class = EndEvent if event_type == "end" else Event
    return event_class(
        id=_read_id(document, "id", place),
        type=event_type,
        trace_id=_read_id(document, "trace_id", place),
        span_id=_read_id(document, "span_id", place),
        time_unix_nano=_read_time(document, place),
        fields=_without(document, EVENT_FIELD_NAMES),
    )


def _parse_start_event(document: dict[str, JsonValue], place: str) -> StartEvent:
    return StartEvent(
        id=_read_id(document, "id", place),
        type="start",
        trace_id=_read_id(document, "trace_id", place),
        span_id=_read_id(document, "span_id", place),
        time_unix_nano=_read_time(document, place),
        parent_span_id=_read_parent_span_id(document, place),
        kind=_read_string(document, "kind", place),
        name=_read_string(document, "name", place),
        seq=_read_seq(document, place),
        fields=_without(document, EVENT_FIELD_NAMES | START_FIELD_NAMES),
    )


def write_event(event: Event) -> dict[str, JsonValue]:
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


def _read_id(document: dict[str, JsonValue], name: str, place: str) -> str:
    value = document.get(name)
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= LARGEST_ID_LENGTH
        or not ID_CHARACTERS.issuperset(value)
    ):
        raise WireFormatError(
            f"{place}.{name}: expected 1 to {LARGEST_ID_LENGTH} of letters, digits, - and _"
        )
    return value


def _read_time(document: dict[str, JsonValue], place: str) -> int:
    value = document.get("time_unix_nano")
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()):
        raise WireFormatError(f"{place}.time_unix_nano: expected a string of digits")
    if len(value) > DIGITS_IN_LARGEST_WHOLE_NUMBER or int(value) > LARGEST_WHOLE_NUMBER:
        raise WireFormatError(f"{place}.time_unix_nano: expected at most {LARGEST_WHOLE_NUMBER}")
    return int(value)


def _read_parent_span_id(document: dict[str, JsonValue], place: str) -> str | None:
    if "parent_span_id" not in document:
        raise WireFormatError(f"{place}.parent_span_id: expected a string or null")
    if document["parent_span_id"] is None:
        return None
    return _read_id(document, "parent_span_id", place)


def _read_seq(document: dict[str, JsonValue], place: str) -> int:
    value = document.get("seq")
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireFormatError(f"{place}.seq: expected a whole number")
    if abs(value) > LARGEST_SEQ:
        raise WireFormatError(f"{place}.seq: expected at most {LARGEST_SEQ}")
    return value


def _without(document: dict[str, JsonValue], names: frozenset[str]) -> dict[str, JsonValue]:
    return {name: value for name, value in document.items() if name not in names}
