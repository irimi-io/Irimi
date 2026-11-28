import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path

from irimi._wire import JsonValue

from irimi_store.flags import read_flags
from irimi_store.stored_fields import split_fields

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
ADD_UNATTACHED_COLUMN = "ALTER TABLE events ADD COLUMN unattached INTEGER NOT NULL DEFAULT 0"
SELECT_ROOT_STARTS = (
    "SELECT arrival, data FROM events WHERE type = 'start' AND parent_span_id IS NULL"
)
MARK_UNATTACHED = "UPDATE events SET unattached = 1 WHERE arrival = ?"
CREATE_ROOTS_INDEX = """
CREATE INDEX events_roots ON events (project, unattached, arrival)
WHERE type = 'start' AND parent_span_id IS NULL
"""
CREATE_RUN_ROOTS_INDEX = """
CREATE INDEX events_run_roots ON events (project, trace_id, seq, arrival)
WHERE type = 'start' AND parent_span_id IS NULL
"""
CREATE_SPANS_INDEX = "CREATE INDEX events_spans ON events (project, trace_id, span_id, type)"
CREATE_RUNS_INDEX = "CREATE INDEX events_runs ON events (project, trace_id)"
HOLD_EVENTS_WITH_DATA = "ALTER TABLE events RENAME TO events_holding_data"
CREATE_EVENTS_TABLE_WITHOUT_DATA = """
CREATE TABLE events (
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
    status TEXT,
    flags TEXT,
    environment TEXT,
    mode TEXT,
    events_produced TEXT,
    data_size INTEGER NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    unattached INTEGER NOT NULL,
    UNIQUE (project, trace_id, id)
)
"""
CREATE_EVENT_DATA_TABLE = """
CREATE TABLE event_data (
    arrival INTEGER PRIMARY KEY,
    data TEXT NOT NULL
)
"""
SELECT_NEXT_EVENT_HOLDING_DATA = """
SELECT
    arrival, project, trace_id, id, type, span_id, time_unix_nano,
    parent_span_id, kind, name, seq, data, created_at_unix_nano, unattached
FROM events_holding_data WHERE arrival > ? ORDER BY arrival LIMIT 1
"""
INSERT_EVENT_WITHOUT_DATA = """
INSERT INTO events (
    arrival, project, trace_id, id, type, span_id, time_unix_nano,
    parent_span_id, kind, name, seq,
    status, flags, environment, mode, events_produced,
    data_size, created_at_unix_nano, unattached
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
INSERT_EVENT_DATA = "INSERT INTO event_data (arrival, data) VALUES (?, ?)"
DROP_EVENTS_HOLDING_DATA = "DROP TABLE events_holding_data"
FIELDS_GIVEN_A_COLUMN = ("status", "flags", "environment", "mode", "events_produced")
UNATTACHED = "unattached"
SECONDS_TO_WAIT_FOR_ANOTHER_OPENER = 30.0
SECONDS_BETWEEN_TRIES = 0.05


EventHoldingData = tuple[
    int, str, str, str, str, str, int, str | None, str | None, str | None, int | None, str, int, int
]


class NewerSchemaError(Exception):
    pass


def is_unattached(fields: Mapping[str, JsonValue]) -> bool:
    return UNATTACHED in read_flags(fields)


def migrate(database_path: Path) -> None:
    with closing(
        sqlite3.connect(
            database_path, isolation_level=None, timeout=SECONDS_TO_WAIT_FOR_ANOTHER_OPENER
        )
    ) as connection:
        version = _read_version(connection)
        if version > len(SCHEMA_STEPS):
            raise NewerSchemaError(
                f"{database_path} has schema version {version},"
                f" and this store knows up to {len(SCHEMA_STEPS)}"
            )
        _switch_to_write_ahead_log(connection)
        while _read_version(connection) < len(SCHEMA_STEPS):
            _run_next_step(connection)


def _switch_to_write_ahead_log(connection: sqlite3.Connection) -> None:
    give_up_at = time.monotonic() + SECONDS_TO_WAIT_FOR_ANOTHER_OPENER
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as error:
            is_held_by_another_opener = error.sqlite_errorcode == sqlite3.SQLITE_BUSY
            if not is_held_by_another_opener or time.monotonic() > give_up_at:
                raise
            time.sleep(SECONDS_BETWEEN_TRIES)
        else:
            return


def _run_next_step(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = _read_version(connection)
        if version < len(SCHEMA_STEPS):
            SCHEMA_STEPS[version](connection)
            connection.execute(f"PRAGMA user_version = {version + 1}")
        connection.execute("COMMIT")
    except sqlite3.Error:
        connection.execute("ROLLBACK")
        raise


def _read_version(connection: sqlite3.Connection) -> int:
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    return int(version)


def _create_events_table(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_EVENTS_TABLE)


def _tell_unattached_roots_apart(connection: sqlite3.Connection) -> None:
    connection.execute(ADD_UNATTACHED_COLUMN)
    root_starts = connection.execute(SELECT_ROOT_STARTS).fetchall()
    connection.executemany(
        MARK_UNATTACHED,
        [(arrival,) for arrival, data in root_starts if is_unattached(json.loads(data))],
    )
    connection.execute(CREATE_ROOTS_INDEX)
    connection.execute(CREATE_RUN_ROOTS_INDEX)
    connection.execute(CREATE_SPANS_INDEX)
    connection.execute(CREATE_RUNS_INDEX)


def _keep_data_apart(connection: sqlite3.Connection) -> None:
    connection.execute(HOLD_EVENTS_WITH_DATA)
    connection.execute(CREATE_EVENTS_TABLE_WITHOUT_DATA)
    connection.execute(CREATE_EVENT_DATA_TABLE)
    last_arrival = 0
    while (row := _read_next_event_holding_data(connection, last_arrival)) is not None:
        last_arrival = _write_event_apart_from_its_data(connection, row)
    connection.execute(DROP_EVENTS_HOLDING_DATA)
    connection.execute(CREATE_ROOTS_INDEX)
    connection.execute(CREATE_RUN_ROOTS_INDEX)
    connection.execute(CREATE_SPANS_INDEX)
    connection.execute(CREATE_RUNS_INDEX)


def _read_next_event_holding_data(
    connection: sqlite3.Connection, last_arrival: int
) -> EventHoldingData | None:
    row: EventHoldingData | None = connection.execute(
        SELECT_NEXT_EVENT_HOLDING_DATA, (last_arrival,)
    ).fetchone()
    return row


def _write_event_apart_from_its_data(connection: sqlite3.Connection, row: EventHoldingData) -> int:
    arrival, data, created_at_unix_nano, unattached = row[0], row[11], row[12], row[13]
    stored = split_fields(json.loads(data), FIELDS_GIVEN_A_COLUMN)
    connection.execute(
        INSERT_EVENT_WITHOUT_DATA,
        (*row[:11], *stored.core, len(stored.data), created_at_unix_nano, unattached),
    )
    connection.execute(INSERT_EVENT_DATA, (arrival, stored.data))
    return arrival


SCHEMA_STEPS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _create_events_table,
    _tell_unattached_roots_apart,
    _keep_data_apart,
)
