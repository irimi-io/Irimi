import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from irimi._wire import Event, parse_event, write_event

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    arrival INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    document TEXT NOT NULL,
    UNIQUE (project, id)
)
"""
CREATE_TRACE_INDEX = """
CREATE INDEX IF NOT EXISTS events_by_trace ON events (project, trace_id)
"""
INSERT_EVENT = """
INSERT OR IGNORE INTO events (project, id, trace_id, document) VALUES (?, ?, ?, ?)
"""
SELECT_RUN_EVENTS = """
SELECT document FROM events WHERE project = ? AND trace_id = ? ORDER BY arrival
"""


class SqliteStorageEngine:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        with self._open_connection() as connection:
            connection.execute(CREATE_EVENTS_TABLE)
            connection.execute(CREATE_TRACE_INDEX)

    def add_events(self, project: str, events: Sequence[Event]) -> None:
        rows = [
            (project, event.id, event.trace_id, json.dumps(write_event(event))) for event in events
        ]
        with self._open_connection() as connection:
            connection.executemany(INSERT_EVENT, rows)

    def read_run_events(self, project: str, trace_id: str) -> tuple[Event, ...]:
        with self._open_connection() as connection:
            rows = connection.execute(SELECT_RUN_EVENTS, (project, trace_id)).fetchall()
        return tuple(parse_event(json.loads(document)) for (document,) in rows)

    @contextmanager
    def _open_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._database_path)) as connection, connection:
            yield connection
