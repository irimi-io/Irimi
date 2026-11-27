from collections.abc import Mapping
from dataclasses import dataclass

from irimi._wire import JsonValue
from irimi._wire.field_names import ENVIRONMENT, FLAGS, MODE, NEXT_CURSOR, STATUS

from irimi_store.pages import Page

READER_API_VERSION = "v1"
RUNS_PATH = f"/api/{READER_API_VERSION}/runs"
RUN_PATH = f"{RUNS_PATH}/{{trace_id}}"
CALL_PATH = f"{RUN_PATH}/calls/{{span_id}}"
UNATTACHED_CALLS_PATH = f"/api/{READER_API_VERSION}/unattached-calls"

RUNS = "runs"
CALLS = "calls"
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
    return {
        **summary.fields,
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
