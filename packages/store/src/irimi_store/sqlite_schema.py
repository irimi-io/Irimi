import json
import sqlite3
from collections.abc import Callable, Mapping

from irimi._wire import JsonValue

from irimi_store.flags import read_flags

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
CREATE INDEX events_roots ON events (project, unattached, time_unix_nano, arrival)
WHERE type = 'start' AND parent_span_id IS NULL
"""
CREATE_SPANS_INDEX = "CREATE INDEX events_spans ON events (project, trace_id, span_id)"
UNATTACHED = "unattached"


def is_unattached(fields: Mapping[str, JsonValue]) -> bool:
    return UNATTACHED in read_flags(fields)


def migrate(connection: sqlite3.Connection) -> None:
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    for step_number in range(version, len(SCHEMA_STEPS)):
        with connection:
            SCHEMA_STEPS[step_number](connection)
            connection.execute(f"PRAGMA user_version = {step_number + 1}")


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
    connection.execute(CREATE_SPANS_INDEX)


SCHEMA_STEPS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _create_events_table,
    _tell_unattached_roots_apart,
)
