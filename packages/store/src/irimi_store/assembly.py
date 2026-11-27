from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from irimi._wire import EndEvent, Event, JsonValue, StartEvent, write_event
from irimi._wire.field_names import (
    BODY,
    DATA,
    ENVIRONMENT,
    EVENTS_PRODUCED,
    FLAGS,
    JSON,
    MODE,
    REQUEST,
    RESPONSE,
    STATUS,
)

from irimi_store.flags import read_flags
from irimi_store.reader_documents import NODE_KEYS, Node, RunSummary, RunTree
from irimi_store.storage import StoredRoot

Condition = Literal["unfinished", "incomplete", "not_replayable"]
NOT_REPLAYABLE = "not_replayable"
FIELDS_THE_NODE_STATES_ITSELF = NODE_KEYS | {STATUS, FLAGS}
FIELDS_WITH_A_BODY = (REQUEST, RESPONSE)
BODY_CONTENT = frozenset({DATA, JSON})


@dataclass(frozen=True)
class Span:
    span_id: str
    arrival: int
    start: StartEvent | None
    end: EndEvent | None
    other_events: tuple[Event, ...]

    @property
    def parent_span_id(self) -> str | None:
        return None if self.start is None else self.start.parent_span_id


def assemble_run(trace_id: str, events: Sequence[Event]) -> RunTree:
    spans = _group_spans(events)
    children = _group_children(spans)
    placed: set[str] = set()
    root_span = _pick_root(spans)
    root = None if root_span is None else _build_subtree(root_span, children, placed)
    detached = [
        _build_subtree(span, children, placed)
        for span in spans.values()
        if span.span_id not in placed and _has_no_parent_in_the_run(span, spans)
    ]
    looping = [_build_node(span, ()) for span in spans.values() if span.span_id not in placed]
    return RunTree(
        id=trace_id,
        conditions=read_conditions(
            None if root_span is None else root_span.start,
            None if root_span is None else root_span.end,
            len(events),
        ),
        root=None if root is None else _omit_bodies(root),
        detached=tuple(_omit_bodies(node) for node in (*detached, *looping)),
    )


def assemble_call(events: Sequence[Event]) -> Node | None:
    spans = _group_spans(events)
    if not spans:
        return None
    return _build_node(next(iter(spans.values())), ())


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


def _group_spans(events: Sequence[Event]) -> dict[str, Span]:
    spans: dict[str, Span] = {}
    for arrival, event in enumerate(events):
        span = spans.get(event.span_id) or Span(event.span_id, arrival, None, None, ())
        spans[event.span_id] = _add_event(span, event)
    return spans


def _add_event(span: Span, event: Event) -> Span:
    if isinstance(event, StartEvent) and span.start is None:
        return replace(span, start=event)
    if isinstance(event, EndEvent) and span.end is None:
        return replace(span, end=event)
    return replace(span, other_events=(*span.other_events, event))


def _group_children(spans: Mapping[str, Span]) -> dict[str, list[Span]]:
    children: dict[str, list[Span]] = {}
    for span in spans.values():
        if span.start is not None and span.start.parent_span_id is not None:
            children.setdefault(span.start.parent_span_id, []).append(span)
    for siblings in children.values():
        siblings.sort(key=_read_position)
    return children


def _read_position(span: Span) -> tuple[int, int]:
    return (0 if span.start is None else span.start.seq, span.arrival)


def _pick_root(spans: Mapping[str, Span]) -> Span | None:
    roots = [
        span for span in spans.values() if span.start is not None and span.parent_span_id is None
    ]
    return min(roots, key=_read_position, default=None)


def _has_no_parent_in_the_run(span: Span, spans: Mapping[str, Span]) -> bool:
    return span.parent_span_id is None or span.parent_span_id not in spans


def _build_subtree(span: Span, children: Mapping[str, list[Span]], placed: set[str]) -> Node:
    placed.add(span.span_id)
    child_nodes = tuple(
        _build_subtree(child, children, placed)
        for child in children.get(span.span_id, ())
        if child.span_id not in placed
    )
    return _build_node(span, child_nodes)


def _build_node(span: Span, children: tuple[Node, ...]) -> Node:
    start_fields: Mapping[str, JsonValue] = {} if span.start is None else span.start.fields
    end_fields: Mapping[str, JsonValue] = {} if span.end is None else span.end.fields
    return Node(
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        kind=None if span.start is None else span.start.kind,
        name=None if span.start is None else span.start.name,
        seq=None if span.start is None else span.start.seq,
        start_time_unix_nano=None if span.start is None else span.start.time_unix_nano,
        end_time_unix_nano=None if span.end is None else span.end.time_unix_nano,
        status=end_fields.get(STATUS),
        flags=merge_flags(start_fields, end_fields),
        fields={
            name: value
            for name, value in {**start_fields, **end_fields}.items()
            if name not in FIELDS_THE_NODE_STATES_ITSELF
        },
        events=tuple(write_event(event) for event in span.other_events),
        children=children,
    )


def _omit_bodies(node: Node) -> Node:
    return replace(
        node,
        fields={
            name: _omit_body(value) if name in FIELDS_WITH_A_BODY else value
            for name, value in node.fields.items()
        },
        children=tuple(_omit_bodies(child) for child in node.children),
    )


def _omit_body(message: JsonValue) -> JsonValue:
    body = message.get(BODY) if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(body, dict):
        return message
    kept = {name: value for name, value in body.items() if name not in BODY_CONTENT}
    return {**message, BODY: kept}
