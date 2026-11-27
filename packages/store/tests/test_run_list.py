import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from irimi_store.sqlite_storage import SqliteStorageEngine

EVENTS_TABLE_BEFORE_SCHEMA_VERSIONS = """
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
    UNIQUE (project, trace_id, id)
)
"""
CONDITION_CASES = [
    ({"events_produced": 3}, {}, ["incomplete"]),
    ({"events_produced": 2}, {"flags": ["not_replayable"]}, ["not_replayable"]),
    ({"events_produced": "2"}, {"flags": "not_replayable"}, []),
    ({}, {}, []),
]


def start(
    trace: str, span: str, name: str, time: int, parent: str | None = None, **fields: Any
) -> dict[str, Any]:
    return {
        "id": f"{trace}-{span}-start",
        "type": "start",
        "trace_id": trace,
        "span_id": span,
        "time_unix_nano": str(time),
        "parent_span_id": parent,
        "kind": "workflow",
        "name": name,
        "seq": 0,
        **fields,
    }


def end(trace: str, span: str, time: int, **fields: Any) -> dict[str, Any]:
    return {
        "id": f"{trace}-{span}-end",
        "type": "end",
        "trace_id": trace,
        "span_id": span,
        "time_unix_nano": str(time),
        **fields,
    }


def test_runs_are_listed_newest_first_apart_from_unattached_calls(store: Any) -> None:
    store.post(
        "/v1/events",
        {
            "events": [
                start("t1", "s1", "older", 100, environment="production", mode="normal", input=[1]),
                end("t1", "s1", 150, status="ok", events_produced=2, output="big"),
                start("t2", "s1", "newer", 200),
                start("t2", "s2", "step", 210, parent="s1"),
                start("t3", "s1", "loose_call", 300, kind="function", flags=["unattached"]),
            ]
        },
    )

    _, runs = store.get("/api/v1/runs")
    _, calls = store.get("/api/v1/unattached-calls")

    assert [run["id"] for run in runs["runs"]] == ["t2", "t1"]
    assert runs["runs"][1] == {
        "id": "t1",
        "span_id": "s1",
        "kind": "workflow",
        "name": "older",
        "start_time_unix_nano": "100",
        "end_time_unix_nano": "150",
        "status": "ok",
        "flags": [],
        "environment": "production",
        "mode": "normal",
        "conditions": [],
    }
    assert runs["runs"][0]["conditions"] == ["unfinished"]
    assert runs["next_cursor"] is None
    assert [(call["id"], call["flags"]) for call in calls["calls"]] == [("t3", ["unattached"])]


@pytest.mark.parametrize(("root_end_fields", "start_fields", "conditions"), CONDITION_CASES)
def test_a_listed_run_states_its_conditions(
    store: Any, root_end_fields: dict[str, Any], start_fields: dict[str, Any], conditions: list[str]
) -> None:
    store.post(
        "/v1/events",
        {
            "events": [
                start("t1", "s1", "run", 100, **start_fields),
                end("t1", "s1", 150, **root_end_fields),
            ]
        },
    )

    assert store.get("/api/v1/runs")[1]["runs"][0]["conditions"] == conditions


def test_run_pages_hold_while_runs_arrive(store: Any) -> None:
    store.post("/v1/events", {"events": [start(f"t{n}", "s1", "run", n * 100) for n in (1, 2, 3)]})
    _, first = store.get("/api/v1/runs?limit=2")
    store.post("/v1/events", {"events": [start("t4", "s1", "run", 400)]})
    _, second = store.get(f"/api/v1/runs?limit=2&cursor={first['next_cursor']}")

    assert [run["id"] for run in first["runs"]] == ["t3", "t2"]
    assert ([run["id"] for run in second["runs"]], second["next_cursor"]) == (["t1"], None)


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "cursor=not-a-cursor"])
def test_a_list_page_request_the_store_cannot_read_is_refused(store: Any, query: str) -> None:
    status, answer = store.get(f"/api/v1/runs?{query}")

    assert (status, bool(answer["error"])) == (400, True)


def test_a_store_file_made_before_schema_versions_lists_its_runs(
    tmp_path: Path, serve_store: Any
) -> None:
    database_path = tmp_path / "older.db"
    connection = sqlite3.connect(database_path)
    connection.execute(EVENTS_TABLE_BEFORE_SCHEMA_VERSIONS)
    connection.executemany(
        "INSERT INTO events (project, trace_id, id, type, span_id, time_unix_nano,"
        " parent_span_id, kind, name, seq, data, created_at_unix_nano)"
        " VALUES ('default', ?, 'e1', 'start', 's1', 100, NULL, ?, ?, 0, ?, 1)",
        [
            ("t1", "workflow", "handle_ticket", json.dumps({"environment": "production"})),
            ("t2", "function", "loose_call", json.dumps({"flags": ["unattached"]})),
        ],
    )
    connection.commit()
    connection.close()

    with serve_store(SqliteStorageEngine(database_path, time.time_ns)) as store:
        _, runs = store.get("/api/v1/runs")
        _, calls = store.get("/api/v1/unattached-calls")

    assert [(run["id"], run["environment"]) for run in runs["runs"]] == [("t1", "production")]
    assert [call["id"] for call in calls["calls"]] == ["t2"]


def test_an_unknown_reader_api_version_is_refused_with_the_accepted_versions(store: Any) -> None:
    status, answer = store.get("/api/v2/runs")

    assert (status, answer["accepted_versions"]) == (404, ["v1"])
