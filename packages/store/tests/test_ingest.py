import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from irimi._wire import write_event
from irimi_store.app import DEFAULT_PROJECT
from irimi_store.sqlite_storage import SqliteStorageEngine
from irimi_store.storage import StorageEngine

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
RUN_NOTE = {
    "id": "e3",
    "type": "added_by_a_newer_sdk",
    "trace_id": "t7c1",
    "span_id": "s1",
    "time_unix_nano": "1795341601000000000",
    "text": "kept",
}


def post(url: str, document: Any) -> tuple[int, Any]:
    body = document if isinstance(document, bytes) else json.dumps(document).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def storage(tmp_path: Path) -> StorageEngine:
    return SqliteStorageEngine(tmp_path / "store.db", time.time_ns)


def read_run(storage: StorageEngine, trace_id: str) -> list[dict[str, Any]]:
    return [write_event(event) for event in storage.read_run_events(DEFAULT_PROJECT, trace_id)]


def test_events_are_kept_as_written_whatever_order_or_repeats_they_arrive_in(
    store_url: str, tmp_path: Path
) -> None:
    first = post(f"{store_url}/v1/events", {"sdk": {"name": "other"}, "events": [RUN_END]})
    second = post(f"{store_url}/v1/events", {"events": [RUN_END, RUN_START, RUN_NOTE]})
    reopened_storage = SqliteStorageEngine(tmp_path / "store.db", time.time_ns)

    assert first == second == (200, {"refused": []})
    assert read_run(reopened_storage, "t7c1") == [RUN_END, RUN_START, RUN_NOTE]
    assert read_run(reopened_storage, "another-run") == []


def test_the_same_event_id_in_two_runs_is_not_a_repeat(
    store_url: str, storage: StorageEngine
) -> None:
    other_run_start = {**RUN_START, "trace_id": "t9d4"}

    post(f"{store_url}/v1/events", {"events": [RUN_START, other_run_start]})

    assert read_run(storage, "t7c1") == [RUN_START]
    assert read_run(storage, "t9d4") == [other_run_start]


def test_a_malformed_event_is_refused_and_the_rest_of_the_batch_is_stored(
    store_url: str, storage: StorageEngine
) -> None:
    status, answer = post(f"{store_url}/v1/events", {"events": [{"id": "e9"}, RUN_START]})

    assert status == 200
    assert [refused["index"] for refused in answer["refused"]] == [0]
    assert read_run(storage, "t7c1") == [RUN_START]


@pytest.mark.parametrize("document", [b"not json", [], {"events": "none"}])
def test_a_malformed_batch_is_refused_with_the_reason(store_url: str, document: Any) -> None:
    status, answer = post(f"{store_url}/v1/events", document)

    assert status == 400
    assert answer["error"]


def test_an_unknown_version_is_refused_with_the_accepted_versions(store_url: str) -> None:
    status, answer = post(f"{store_url}/v2/events", {"events": [RUN_START]})

    assert status == 404
    assert answer["accepted_versions"] == ["v1"]
