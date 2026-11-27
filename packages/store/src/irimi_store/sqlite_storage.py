import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from irimi._wire import EndEvent, Event, JsonValue, StartEvent, parse_event
from irimi._wire.events import DIGITS_IN_LARGEST_WHOLE_NUMBER, LARGEST_WHOLE_NUMBER

from irimi_store.pages import InvalidCursorError, Page
from irimi_store.sqlite_schema import is_unattached, migrate
from irimi_store.storage import RootSet, StoredRoot

INSERT_EVENT = """
INSERT OR IGNORE INTO events (
    project, trace_id, id, type, span_id, time_unix_nano,
    parent_span_id, kind, name, seq, data, created_at_unix_nano, unattached
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
EVENT_COLUMNS = "id, type, trace_id, span_id, time_unix_nano, parent_span_id, kind, name, seq, data"
SELECT_RUN_EVENTS = f"""
SELECT {EVENT_COLUMNS}, arrival
FROM events WHERE project = ? AND trace_id = ? AND arrival > ? ORDER BY arrival LIMIT ?
"""
SELECT_ROOTS = f"""
SELECT {EVENT_COLUMNS}, arrival, (
    SELECT COUNT(*) FROM events held
    WHERE held.project = events.project AND held.trace_id = events.trace_id
        AND held.type IN ('start', 'end')
)
FROM events
WHERE project = ? AND unattached = ? AND type = 'start' AND parent_span_id IS NULL
    AND arrival < ?
    AND NOT EXISTS (
        SELECT 1 FROM events other
        WHERE other.project = events.project AND other.trace_id = events.trace_id
            AND other.type = 'start' AND other.parent_span_id IS NULL
            AND (other.seq, other.arrival) < (events.seq, events.arrival)
    )
ORDER BY arrival DESC LIMIT ?
"""
SELECT_SPAN_EVENTS = f"""
SELECT {EVENT_COLUMNS} FROM events
WHERE project = ? AND trace_id = ? AND span_id = ? ORDER BY arrival
"""
SELECT_SPAN_END = f"""
SELECT {EVENT_COLUMNS} FROM events
WHERE project = ? AND trace_id = ? AND span_id = ? AND type = 'end' ORDER BY arrival LIMIT 1
"""

EventRow = tuple[str, str, str, str, int, str | None, str | None, str | None, int | None, str]
ArrivedEventRow = tuple[
    str, str, str, str, int, str | None, str | None, str | None, int | None, str, int
]
RootRow = tuple[
    str, str, str, str, int, str | None, str | None, str | None, int | None, str, int, int
]
StartColumns = tuple[str | None, str | None, str | None, int | None]


class SqliteStorageEngine:
    def __init__(self, database_path: Path, read_clock: Callable[[], int]) -> None:
        self._database_path = database_path
        self._read_clock = read_clock
        migrate(database_path)

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
                is_unattached(event.fields),
            )
            for event in events
        ]
        with self._open_connection() as connection:
            connection.executemany(INSERT_EVENT, rows)

    def read_run_events(
        self, project: str, trace_id: str, cursor: str | None, limit: int
    ) -> Page[Event]:
        after_arrival = _read_arrival_cursor(cursor, 0)
        with self._open_connection() as connection:
            rows: list[ArrivedEventRow] = connection.execute(
                SELECT_RUN_EVENTS, (project, trace_id, after_arrival, limit + 1)
            ).fetchall()
        shown = rows[:limit]
        return Page(
            items=tuple(_read_event(row[:10]) for row in shown),
            next_cursor=str(shown[-1][10]) if len(rows) > limit else None,
        )

    def read_call_events(self, project: str, trace_id: str, span_id: str) -> tuple[Event, ...]:
        with self._open_connection() as connection:
            rows: list[EventRow] = connection.execute(
                SELECT_SPAN_EVENTS, (project, trace_id, span_id)
            ).fetchall()
        return tuple(_read_event(row) for row in rows)

    def list_roots(
        self, project: str, root_set: RootSet, cursor: str | None, limit: int
    ) -> Page[StoredRoot]:
        before_arrival = _read_arrival_cursor(cursor, LARGEST_WHOLE_NUMBER)
        with self._open_connection() as connection:
            rows: list[RootRow] = connection.execute(
                SELECT_ROOTS, (project, root_set == "unattached_calls", before_arrival, limit + 1)
            ).fetchall()
            shown = rows[:limit]
            roots = tuple(_read_root(connection, project, row) for row in shown)
        return Page(
            items=roots,
            next_cursor=str(shown[-1][10]) if len(rows) > limit else None,
        )

    @contextmanager
    def _open_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._database_path)) as connection, connection:
            yield connection


def _read_arrival_cursor(cursor: str | None, arrival_on_the_first_page: int) -> int:
    if cursor is None:
        return arrival_on_the_first_page
    is_whole_number = cursor.isascii() and cursor.isdigit()
    if not is_whole_number or len(cursor) > DIGITS_IN_LARGEST_WHOLE_NUMBER:
        raise InvalidCursorError("cursor: not one this store issued")
    if int(cursor) > LARGEST_WHOLE_NUMBER:
        raise InvalidCursorError("cursor: not one this store issued")
    return int(cursor)


def _read_root(connection: sqlite3.Connection, project: str, row: RootRow) -> StoredRoot:
    start = _read_event(row[:10])
    end_row: EventRow | None = connection.execute(
        SELECT_SPAN_END, (project, start.trace_id, start.span_id)
    ).fetchone()
    end = None if end_row is None else _read_event(end_row)
    if not isinstance(start, StartEvent) or not isinstance(end, EndEvent | None):
        raise TypeError(f"stored root {start.trace_id} is not a start with an end")
    return StoredRoot(start=start, end=end, events_held=row[11])


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
