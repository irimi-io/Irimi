import json
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from irimi._wire import write_event
from irimi_store.app import DEFAULT_PROJECT
from irimi_store.sqlite_storage import SqliteStorageEngine
from irimi_store.storage import StorageEngine

StartStore = Callable[[StorageEngine], AbstractContextManager[str]]

RUN_START = {
    "id": "e1",
    "type": "start",
    "trace_id": "t7c1",
    "span_id": "s1",
    "time_unix_nano": "1795341600000000000",
    "parent_span_id": None,
    "kind": "workflow",
    "name": "handle_ticket",
    "seq": 0,
    "input": {"args": [{"id": "T-1042"}], "kwargs": {}},
    "added_by_a_newer_sdk": {"anything": [1, 2]},
}
RUN_END = {
    "id": "e2",
    "type": "end",
    "trace_id": "t7c1",
    "span_id": "s1",
    "time_unix_nano": "1795341602340000000",
    "status": "ok",
    "events_produced": 2,
}


def post(url: str, document: Any) -> tuple[int, Any]:
    body = document if isinstance(document, bytes) else json.dumps(document).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def read_run(storage: StorageEngine, trace_id: str) -> list[dict[str, Any]]:
    return [write_event(event) for event in storage.read_run_events(DEFAULT_PROJECT, trace_id)]


def test_events_are_kept_as_written_whatever_order_or_repeats_they_arrive_in(
    start_store: StartStore, tmp_path: Path
) -> None:
    database_path = tmp_path / "store.db"
    with start_store(SqliteStorageEngine(database_path)) as store_url:
        first = post(f"{store_url}/v1/events", {"sdk": {"name": "other"}, "events": [RUN_END]})
        second = post(f"{store_url}/v1/events", {"events": [RUN_END, RUN_START]})

    assert first == second == (200, {"refused": []})
    assert read_run(SqliteStorageEngine(database_path), "t7c1") == [RUN_END, RUN_START]
    assert read_run(SqliteStorageEngine(database_path), "another-run") == []


def test_a_malformed_event_is_refused_and_the_rest_of_the_batch_is_stored(
    start_store: StartStore, tmp_path: Path
) -> None:
    storage = SqliteStorageEngine(tmp_path / "store.db")
    with start_store(storage) as store_url:
        status, answer = post(f"{store_url}/v1/events", {"events": [{"id": "e9"}, RUN_START]})

    assert status == 200
    assert [refused["index"] for refused in answer["refused"]] == [0]
    assert read_run(storage, "t7c1") == [RUN_START]


@pytest.mark.parametrize("document", [b"not json", [], {"events": "none"}])
def test_a_malformed_batch_is_refused_with_the_reason(
    start_store: StartStore, tmp_path: Path, document: Any
) -> None:
    with start_store(SqliteStorageEngine(tmp_path / "store.db")) as store_url:
        status, answer = post(f"{store_url}/v1/events", document)

    assert status == 400
    assert answer["error"]


def test_an_unknown_version_is_refused_with_the_accepted_versions(
    start_store: StartStore, tmp_path: Path
) -> None:
    with start_store(SqliteStorageEngine(tmp_path / "store.db")) as store_url:
        status, answer = post(f"{store_url}/v2/events", {"events": [RUN_START]})

    assert status == 404
    assert answer["accepted_versions"] == ["v1"]
