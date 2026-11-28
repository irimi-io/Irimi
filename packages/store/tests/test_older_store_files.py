import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from irimi_store.sqlite_storage import SqliteStorageEngine

RECORDED_RUN = json.loads(
    (Path(__file__).parents[2] / "sdk/tests/fixtures/recorded_run.json").read_text()
)["events"]
LOOSE_CALL = {
    "id": "u1",
    "type": "start",
    "trace_id": "t9",
    "span_id": "s1",
    "time_unix_nano": "1795341700000000000",
    "parent_span_id": None,
    "kind": "http",
    "name": "GET example.com/",
    "seq": 0,
    "flags": ["unattached"],
    "status": None,
}
LATE_NOTE = {
    "id": "e9",
    "type": "added_by_a_newer_sdk",
    "trace_id": "t7c1",
    "span_id": "s1",
    "time_unix_nano": "1795341603000000000",
    "status": True,
}
COLUMNS_AT_SCHEMA_VERSION_2 = (
    "trace_id",
    "id",
    "type",
    "span_id",
    "time_unix_nano",
    "parent_span_id",
    "kind",
    "name",
    "seq",
)
STORE_FILE_AT_SCHEMA_VERSION_2 = """
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
    data TEXT NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    unattached INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project, trace_id, id)
);
CREATE INDEX events_roots ON events (project, unattached, arrival)
WHERE type = 'start' AND parent_span_id IS NULL;
CREATE INDEX events_run_roots ON events (project, trace_id, seq, arrival)
WHERE type = 'start' AND parent_span_id IS NULL;
CREATE INDEX events_spans ON events (project, trace_id, span_id, type);
CREATE INDEX events_runs ON events (project, trace_id);
PRAGMA user_version = 2;
"""


def make_store_file_at_schema_version_2(database_path: Path, events: list[dict[str, Any]]) -> None:
    connection = sqlite3.connect(database_path)
    connection.executescript(STORE_FILE_AT_SCHEMA_VERSION_2)
    connection.executemany(
        "INSERT INTO events (project, trace_id, id, type, span_id, time_unix_nano,"
        " parent_span_id, kind, name, seq, data, created_at_unix_nano, unattached)"
        " VALUES ('default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        [
            (
                *(event.get(column) for column in COLUMNS_AT_SCHEMA_VERSION_2),
                json.dumps(
                    {
                        name: value
                        for name, value in event.items()
                        if name not in COLUMNS_AT_SCHEMA_VERSION_2
                    }
                ),
                "unattached" in event.get("flags", []),
            )
            for event in events
        ],
    )
    connection.commit()
    connection.close()


def test_a_store_file_from_before_data_was_kept_apart_reads_as_it_did(
    tmp_path: Path, serve_store: Any
) -> None:
    database_path = tmp_path / "older.db"
    make_store_file_at_schema_version_2(database_path, [*RECORDED_RUN, LOOSE_CALL])

    with serve_store(SqliteStorageEngine(database_path, time.time_ns)) as store:
        _, replay = store.get("/v1/runs/t7c1/events")
        _, loose = store.get("/v1/runs/t9/events")
        _, runs = store.get("/api/v1/runs")
        _, calls = store.get("/api/v1/unattached-calls")
        _, tree = store.get("/api/v1/runs/t7c1")
        _, call = store.get("/api/v1/runs/t7c1/calls/s3")
        store.post("/v1/events", {"events": [LATE_NOTE, RECORDED_RUN[0]]})
        _, grown = store.get(f"/v1/runs/t7c1/events?cursor={len(RECORDED_RUN)}")
    with serve_store(SqliteStorageEngine(database_path, time.time_ns)) as reopened:
        _, read_again = reopened.get("/v1/runs/t7c1/events")

    assert replay == {"events": RECORDED_RUN, "next_cursor": None}
    assert loose["events"] == [LOOSE_CALL]
    assert [
        (run["id"], run["status"], run["environment"], run["mode"], run["conditions"])
        for run in runs["runs"]
    ] == [("t7c1", "ok", "production", "normal", [])]
    assert [(call["id"], call["flags"]) for call in calls["runs"]] == [("t9", ["unattached"])]
    assert [child["kind"] for child in tree["run"]["root"]["children"]] == [
        "llm",
        "http",
        "function",
    ]
    assert call["call"]["response"]["body"]["json"] == {"id": "re_456", "status": "succeeded"}
    assert grown == {"events": [LATE_NOTE], "next_cursor": None}
    assert read_again["events"] == [*RECORDED_RUN, LATE_NOTE]
