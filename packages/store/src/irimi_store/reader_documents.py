from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeVar

from irimi._wire import JsonValue
from irimi._wire.field_names import ENVIRONMENT, FLAGS, MODE, NEXT_CURSOR, STATUS

from irimi_store.pages import Page

Optional = TypeVar("Optional", str, int)

READER_API_VERSION = "v1"
RUNS_PATH = f"/api/{READER_API_VERSION}/runs"
RUN_PATH = f"{RUNS_PATH}/{{trace_id}}"
CALL_PATH = f"{RUN_PATH}/calls/{{span_id}}"
UNATTACHED_CALLS_PATH = f"/api/{READER_API_VERSION}/unattached-calls"

RUNS = "runs"
RUN = "run"
CALLS = "calls"
CALL = "call"
ROOT = "root"
DETACHED = "detached"
PARENT_SPAN_ID = "parent_span_id"
SEQ = "seq"
CHILDREN = "children"
EVENTS = "events"
ID = "id"
SPAN_ID = "span_id"
KIND = "kind"
NAME = "name"
START_TIME = "start_time_unix_nano"
END_TIME = "end_time_unix_nano"
CONDITIONS = "conditions"
RUN_SUMMARY_KEYS = frozenset(
    {ID, SPAN_ID, KIND, NAME, START_TIME, END_TIME, STATUS, FLAGS, ENVIRONMENT, MODE, CONDITIONS}
)
NODE_KEYS = frozenset(
    {
        SPAN_ID,
        PARENT_SPAN_ID,
        KIND,
        NAME,
        SEQ,
        START_TIME,
        END_TIME,
        STATUS,
        FLAGS,
        CHILDREN,
        EVENTS,
    }
)


class ReaderDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class RunSummary:
    id: str
    span_id: str
    kind: str
    name: str
    start_time_unix_nano: int
    end_time_unix_nano: int | None
    status: JsonValue
    flags: tuple[JsonValue, ...]
    environment: JsonValue
    mode: JsonValue
    conditions: tuple[str, ...]
    fields: Mapping[str, JsonValue]


@dataclass(frozen=True)
class Node:
    span_id: str
    parent_span_id: str | None
    kind: str | None
    name: str | None
    seq: int | None
    start_time_unix_nano: int | None
    end_time_unix_nano: int | None
    status: JsonValue
    flags: tuple[JsonValue, ...]
    fields: Mapping[str, JsonValue]
    events: tuple[Mapping[str, JsonValue], ...]
    children: tuple[Node, ...]


@dataclass(frozen=True)
class RunTree:
    id: str
    conditions: tuple[str, ...]
    root: Node | None
    detached: tuple[Node, ...]


def write_run(run: RunTree) -> dict[str, JsonValue]:
    detached: list[JsonValue] = [_write_node(node) for node in run.detached]
    return {
        RUN: {
            ID: run.id,
            CONDITIONS: list(run.conditions),
            ROOT: None if run.root is None else _write_node(run.root),
            DETACHED: detached,
        }
    }


def parse_run(document: JsonValue) -> RunTree:
    run = document.get(RUN) if isinstance(document, dict) else None
    if not isinstance(run, dict):
        raise ReaderDocumentError(f"{RUN}: expected an object")
    root = run.get(ROOT)
    return RunTree(
        id=read_string(run, ID, RUN),
        conditions=tuple(str(condition) for condition in read_list(run, CONDITIONS, RUN)),
        root=None if root is None else _parse_node(root, f"{RUN}.{ROOT}"),
        detached=_parse_nodes(run, DETACHED, RUN),
    )


def write_call(node: Node) -> dict[str, JsonValue]:
    call = _write_node(node)
    del call[CHILDREN]
    return {CALL: call}


def write_run_page(page: Page[RunSummary]) -> dict[str, JsonValue]:
    return _write_summary_page(RUNS, page)


def write_unattached_call_page(page: Page[RunSummary]) -> dict[str, JsonValue]:
    return _write_summary_page(CALLS, page)


def parse_run_page(document: JsonValue) -> Page[RunSummary]:
    if not isinstance(document, dict):
        raise ReaderDocumentError("run page: expected an object")
    runs = document.get(RUNS)
    next_cursor = document.get(NEXT_CURSOR)
    if not isinstance(runs, list):
        raise ReaderDocumentError(f"run page.{RUNS}: expected a list")
    if not isinstance(next_cursor, str | None):
        raise ReaderDocumentError(f"run page.{NEXT_CURSOR}: expected a string or null")
    return Page(
        items=tuple(_parse_run_summary(run, f"{RUNS}[{place}]") for place, run in enumerate(runs)),
        next_cursor=next_cursor,
    )


def write_time(time_unix_nano: int | None) -> str | None:
    return None if time_unix_nano is None else str(time_unix_nano)


def read_time(document: dict[str, JsonValue], name: str, place: str) -> int | None:
    value = document.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()):
        raise ReaderDocumentError(f"{place}.{name}: expected a string of digits or null")
    return int(value)


def read_string(document: dict[str, JsonValue], name: str, place: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise ReaderDocumentError(f"{place}.{name}: expected a string")
    return value


def read_list(document: dict[str, JsonValue], name: str, place: str) -> list[JsonValue]:
    value = document.get(name)
    if not isinstance(value, list):
        raise ReaderDocumentError(f"{place}.{name}: expected a list")
    return value


def _write_summary_page(name: str, page: Page[RunSummary]) -> dict[str, JsonValue]:
    summaries: list[JsonValue] = [_write_run_summary(summary) for summary in page.items]
    return {name: summaries, NEXT_CURSOR: page.next_cursor}


def _write_run_summary(summary: RunSummary) -> dict[str, JsonValue]:
    core: dict[str, JsonValue] = {
        ID: summary.id,
        SPAN_ID: summary.span_id,
        KIND: summary.kind,
        NAME: summary.name,
        START_TIME: write_time(summary.start_time_unix_nano),
        END_TIME: write_time(summary.end_time_unix_nano),
        STATUS: summary.status,
        FLAGS: list(summary.flags),
        ENVIRONMENT: summary.environment,
        MODE: summary.mode,
        CONDITIONS: list(summary.conditions),
    }
    return _add_other_fields(core, summary.fields)


def _add_other_fields(
    core: dict[str, JsonValue], fields: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    return {**core, **{name: value for name, value in fields.items() if name not in core}}


def _parse_run_summary(document: JsonValue, place: str) -> RunSummary:
    if not isinstance(document, dict):
        raise ReaderDocumentError(f"{place}: expected an object")
    start_time = read_time(document, START_TIME, place)
    if start_time is None:
        raise ReaderDocumentError(f"{place}.{START_TIME}: expected a string of digits")
    return RunSummary(
        id=read_string(document, ID, place),
        span_id=read_string(document, SPAN_ID, place),
        kind=read_string(document, KIND, place),
        name=read_string(document, NAME, place),
        start_time_unix_nano=start_time,
        end_time_unix_nano=read_time(document, END_TIME, place),
        status=document.get(STATUS),
        flags=tuple(read_list(document, FLAGS, place)),
        environment=document.get(ENVIRONMENT),
        mode=document.get(MODE),
        conditions=tuple(str(condition) for condition in read_list(document, CONDITIONS, place)),
        fields={name: value for name, value in document.items() if name not in RUN_SUMMARY_KEYS},
    )


def _write_node(node: Node) -> dict[str, JsonValue]:
    children: list[JsonValue] = [_write_node(child) for child in node.children]
    events: list[JsonValue] = [dict(event) for event in node.events]
    core: dict[str, JsonValue] = {
        SPAN_ID: node.span_id,
        PARENT_SPAN_ID: node.parent_span_id,
        KIND: node.kind,
        NAME: node.name,
        SEQ: node.seq,
        START_TIME: write_time(node.start_time_unix_nano),
        END_TIME: write_time(node.end_time_unix_nano),
        STATUS: node.status,
        FLAGS: list(node.flags),
        CHILDREN: children,
        EVENTS: events,
    }
    return _add_other_fields(core, node.fields)


def _parse_node(document: JsonValue, place: str) -> Node:
    if not isinstance(document, dict):
        raise ReaderDocumentError(f"{place}: expected an object")
    events = read_list(document, EVENTS, place)
    return Node(
        span_id=read_string(document, SPAN_ID, place),
        parent_span_id=_read_optional(document, PARENT_SPAN_ID, place, str),
        kind=_read_optional(document, KIND, place, str),
        name=_read_optional(document, NAME, place, str),
        seq=_read_optional(document, SEQ, place, int),
        start_time_unix_nano=read_time(document, START_TIME, place),
        end_time_unix_nano=read_time(document, END_TIME, place),
        status=document.get(STATUS),
        flags=tuple(read_list(document, FLAGS, place)),
        fields={name: value for name, value in document.items() if name not in NODE_KEYS},
        events=tuple(event for event in events if isinstance(event, dict)),
        children=_parse_nodes(document, CHILDREN, place),
    )


def _parse_nodes(document: dict[str, JsonValue], name: str, place: str) -> tuple[Node, ...]:
    nodes = read_list(document, name, place)
    return tuple(_parse_node(node, f"{place}.{name}[{index}]") for index, node in enumerate(nodes))


def _read_optional(
    document: dict[str, JsonValue], name: str, place: str, expected: type[Optional]
) -> Optional | None:
    value = document.get(name)
    if value is None or (isinstance(value, expected) and not isinstance(value, bool)):
        return value
    raise ReaderDocumentError(f"{place}.{name}: expected {expected.__name__} or null")
