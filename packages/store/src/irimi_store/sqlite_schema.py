import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path

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
CREATE INDEX events_roots ON events (project, unattached, arrival)
WHERE type = 'start' AND parent_span_id IS NULL
"""
CREATE_RUN_ROOTS_INDEX = """
CREATE INDEX events_run_roots ON events (project, trace_id, seq, arrival)
WHERE type = 'start' AND parent_span_id IS NULL
"""
CREATE_SPANS_INDEX = "CREATE INDEX events_spans ON events (project, trace_id, span_id, type)"
CREATE_RUNS_INDEX = "CREATE INDEX events_runs ON events (project, trace_id)"
UNATTACHED = "unattached"
SECONDS_TO_WAIT_FOR_ANOTHER_OPENER = 30.0
SECONDS_BETWEEN_TRIES = 0.05


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


SCHEMA_STEPS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _create_events_table,
    _tell_unattached_roots_apart,
)
