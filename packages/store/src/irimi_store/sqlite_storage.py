import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from irimi._wire import EndEvent, Event, JsonValue, StartEvent, parse_event
from irimi._wire.events import DIGITS_IN_LARGEST_WHOLE_NUMBER, LARGEST_WHOLE_NUMBER

from irimi_store.pages import InvalidCursorError, Page, count_items_that_fit
from irimi_store.sqlite_schema import is_unattached, migrate
from irimi_store.storage import CORE_FIELD_NAMES, RootSet, StoredRoot
from irimi_store.stored_fields import StoredFields, join_fields, split_fields

CORE_FIELD_COLUMNS = ", ".join(CORE_FIELD_NAMES)
INSERT_EVENT = f"""
INSERT OR IGNORE INTO events (
    project, trace_id, id, type, span_id, time_unix_nano,
    parent_span_id, kind, name, seq, {CORE_FIELD_COLUMNS},
    data_size, created_at_unix_nano, unattached
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {", ".join("?" for _ in CORE_FIELD_NAMES)}, ?, ?, ?)
"""
INSERT_EVENT_DATA = "INSERT INTO event_data (arrival, data) VALUES (?, ?)"
CORE_EVENT_COLUMNS = f"""
    id, type, trace_id, span_id, time_unix_nano, parent_span_id, kind, name, seq,
    {CORE_FIELD_COLUMNS}
"""
EVENT_COLUMNS = f"{CORE_EVENT_COLUMNS}, data"
EVENTS_WITH_DATA = "events JOIN event_data USING (arrival)"
SELECT_RUN_EVENT_SIZES = """
SELECT arrival, data_size FROM events
WHERE project = ? AND trace_id = ? AND arrival > ? ORDER BY arrival LIMIT ?
"""
SELECT_RUN_EVENTS = f"""
SELECT {EVENT_COLUMNS} FROM {EVENTS_WITH_DATA}
WHERE project = ? AND trace_id = ? AND arrival > ? AND arrival <= ? ORDER BY arrival
"""
SELECT_RUN_CORE_EVENTS = f"""
SELECT {CORE_EVENT_COLUMNS} FROM events WHERE project = ? AND trace_id = ? ORDER BY arrival
"""
SELECT_ROOTS = f"""
SELECT {CORE_EVENT_COLUMNS}, arrival, (
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
SELECT {EVENT_COLUMNS} FROM {EVENTS_WITH_DATA}
WHERE project = ? AND trace_id = ? AND span_id = ? ORDER BY arrival
"""
SELECT_SPAN_END = f"""
SELECT {CORE_EVENT_COLUMNS} FROM events
WHERE project = ? AND trace_id = ? AND span_id = ? AND type = 'end' ORDER BY arrival LIMIT 1
"""

CoreEventRow = tuple[
    str,
    str,
    str,
    str,
    int,
    str | None,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]
EventRow = tuple[
    str,
    str,
    str,
    str,
    int,
    str | None,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str,
]
RootRow = tuple[
    str,
    str,
    str,
    str,
    int,
    str | None,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    int,
    int,
]
EventColumns = tuple[str, str, str, str, int, str | None, str | None, str | None, int | None]
StartColumns = tuple[str | None, str | None, str | None, int | None]


class SqliteStorageEngine:
    def __init__(self, database_path: Path, read_clock: Callable[[], int]) -> None:
        self._database_path = database_path
        self._read_clock = read_clock
        migrate(database_path)

    def add_events(self, project: str, events: Sequence[Event]) -> None:
        created_at = self._read_clock()
        stored = [split_fields(event.fields, CORE_FIELD_NAMES) for event in events]
        with self._open_connection() as connection:
            for event, stored_fields in zip(events, stored, strict=True):
                _add_event(connection, project, event, stored_fields, created_at)

    def read_run_events(
        self, project: str, trace_id: str, cursor: str | None, limit: int, size_limit: int
    ) -> Page[Event]:
        after_arrival = _read_arrival_cursor(cursor, 0)
        with self._open_connection() as connection:
            sizes: list[tuple[int, int]] = connection.execute(
                SELECT_RUN_EVENT_SIZES, (project, trace_id, after_arrival, limit + 1)
            ).fetchall()
            shown = sizes[: count_items_that_fit([size for _, size in sizes], limit, size_limit)]
            if not shown:
                return Page(items=(), next_cursor=None)
            last_arrival = shown[-1][0]
            rows: list[EventRow] = connection.execute(
                SELECT_RUN_EVENTS, (project, trace_id, after_arrival, last_arrival)
            ).fetchall()
        return Page(
            items=tuple(_read_event(row) for row in rows),
            next_cursor=str(last_arrival) if len(sizes) > len(shown) else None,
        )

    def read_run_core_events(self, project: str, trace_id: str) -> tuple[Event, ...]:
        with self._open_connection() as connection:
            rows: list[CoreEventRow] = connection.execute(
                SELECT_RUN_CORE_EVENTS, (project, trace_id)
            ).fetchall()
        return tuple(_read_core_event(row) for row in rows)

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
            next_cursor=str(shown[-1][14]) if len(rows) > limit else None,
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
    start = _read_core_event(row[:14])
    end_row: CoreEventRow | None = connection.execute(
        SELECT_SPAN_END, (project, start.trace_id, start.span_id)
    ).fetchone()
    end = None if end_row is None else _read_core_event(end_row)
    if not isinstance(start, StartEvent) or not isinstance(end, EndEvent | None):
        raise TypeError(f"stored root {start.trace_id} is not a start with an end")
    return StoredRoot(start=start, end=end, events_held=row[15])


def _add_event(
    connection: sqlite3.Connection,
    project: str,
    event: Event,
    stored_fields: StoredFields,
    created_at: int,
) -> None:
    added = connection.execute(
        INSERT_EVENT,
        (
            project,
            event.trace_id,
            event.id,
            event.type,
            event.span_id,
            event.time_unix_nano,
            *_write_start_columns(event),
            *stored_fields.core,
            len(stored_fields.data),
            created_at,
            is_unattached(event.fields),
        ),
    )
    if added.rowcount == 1:
        connection.execute(INSERT_EVENT_DATA, (added.lastrowid, stored_fields.data))


def _write_start_columns(event: Event) -> StartColumns:
    if not isinstance(event, StartEvent):
        return (None, None, None, None)
    return (event.parent_span_id, event.kind, event.name, event.seq)


def _read_event(row: EventRow) -> Event:
    return _build_event(row[:9], StoredFields(core=row[9:14], data=row[14]))


def _read_core_event(row: CoreEventRow) -> Event:
    return _build_event(row[:9], StoredFields(core=row[9:], data="{}"))


def _build_event(columns: EventColumns, stored_fields: StoredFields) -> Event:
    event_id, event_type, trace_id, span_id, time_unix_nano, parent_span_id, kind, name, seq = (
        columns
    )
    document: dict[str, JsonValue] = {
        **join_fields(stored_fields, CORE_FIELD_NAMES),
        "id": event_id,
        "type": event_type,
        "trace_id": trace_id,
        "span_id": span_id,
        "time_unix_nano": str(time_unix_nano),
    }
    if kind is not None:
        document.update(parent_span_id=parent_span_id, kind=kind, name=name, seq=seq)
    return parse_event(document)
