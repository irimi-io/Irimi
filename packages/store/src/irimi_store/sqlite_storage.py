import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from irimi._wire import Event, JsonValue, StartEvent, parse_event

from irimi_store.pages import InvalidCursorError, Page

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    arrival INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    type TEXT NOT NULL,
    span_id TEXT NOT NULL,
    time_unix_nano INTEGER NOT NULL,
    parent_span_id TEXT,
    kind TEXT,
    name TEXT,
    seq INTEGER,
    data TEXT NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    UNIQUE (project, trace_id, id)
)
"""
INSERT_EVENT = """
INSERT OR IGNORE INTO events (
    project, trace_id, id, type, span_id, time_unix_nano,
    parent_span_id, kind, name, seq, data, created_at_unix_nano
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
EVENT_COLUMNS = "id, type, trace_id, span_id, time_unix_nano, parent_span_id, kind, name, seq, data"
SELECT_RUN_EVENTS = f"""
SELECT {EVENT_COLUMNS}, arrival
FROM events WHERE project = ? AND trace_id = ? AND arrival > ? ORDER BY arrival LIMIT ?
"""

EventRow = tuple[str, str, str, str, int, str | None, str | None, str | None, int | None, str]
ArrivedEventRow = tuple[
    str, str, str, str, int, str | None, str | None, str | None, int | None, str, int
]
StartColumns = tuple[str | None, str | None, str | None, int | None]


class SqliteStorageEngine:
    def __init__(self, database_path: Path, read_clock: Callable[[], int]) -> None:
        self._database_path = database_path
        self._read_clock = read_clock
        with self._open_connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(CREATE_EVENTS_TABLE)

    def add_events(self, project: str, events: Sequence[Event]) -> None:
        created_at = self._read_clock()
        rows = [
            (
                project,
                event.trace_id,
                event.id,
                event.type,
                event.span_id,
                event.time_unix_nano,
                *_write_start_columns(event),
                json.dumps(event.fields),
                created_at,
            )
            for event in events
        ]
        with self._open_connection() as connection:
            connection.executemany(INSERT_EVENT, rows)

    def read_run_events(
        self, project: str, trace_id: str, cursor: str | None, limit: int
    ) -> Page[Event]:
        after_arrival = _read_arrival_cursor(cursor)
        with self._open_connection() as connection:
            rows: list[ArrivedEventRow] = connection.execute(
                SELECT_RUN_EVENTS, (project, trace_id, after_arrival, limit + 1)
            ).fetchall()
        shown = rows[:limit]
        return Page(
            items=tuple(_read_event(row[:10]) for row in shown),
            next_cursor=str(shown[-1][10]) if len(rows) > limit else None,
        )

    @contextmanager
    def _open_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._database_path)) as connection, connection:
            yield connection


def _read_arrival_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not (cursor.isascii() and cursor.isdigit()):
        raise InvalidCursorError("cursor: not one this store issued")
    return int(cursor)


def _write_start_columns(event: Event) -> StartColumns:
    if not isinstance(event, StartEvent):
        return (None, None, None, None)
    return (event.parent_span_id, event.kind, event.name, event.seq)


def _read_event(row: EventRow) -> Event:
    (
        event_id,
        event_type,
        trace_id,
        span_id,
        time_unix_nano,
        parent_span_id,
        kind,
        name,
        seq,
        data,
    ) = row
    document: dict[str, JsonValue] = {
        **json.loads(data),
        "id": event_id,
        "type": event_type,
        "trace_id": trace_id,
        "span_id": span_id,
        "time_unix_nano": str(time_unix_nano),
    }
    if kind is not None:
        document.update(parent_span_id=parent_span_id, kind=kind, name=name, seq=seq)
    return parse_event(document)
