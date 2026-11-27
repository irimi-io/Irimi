from typing import Any

import pytest

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


def read_run(store: Any, trace_id: str) -> Any:
    return store.get(f"/v1/runs/{trace_id}/events")[1]["events"]


def test_events_are_kept_as_written_whatever_order_or_repeats_they_arrive_in(
    store: Any,
) -> None:
    first = store.post("/v1/events", {"sdk": {"name": "other"}, "events": [RUN_END]})
    second = store.post("/v1/events", {"events": [RUN_END, RUN_START, RUN_NOTE]})

    assert first == second == (200, {"refused": []})
    assert read_run(store, "t7c1") == [RUN_END, RUN_START, RUN_NOTE]


def test_the_same_event_id_in_two_runs_is_not_a_repeat(store: Any) -> None:
    other_run_start = {**RUN_START, "trace_id": "t9d4"}

    store.post("/v1/events", {"events": [RUN_START, other_run_start]})

    assert read_run(store, "t7c1") == [RUN_START]
    assert read_run(store, "t9d4") == [other_run_start]


def test_a_malformed_event_is_refused_and_the_rest_of_the_batch_is_stored(
    store: Any,
) -> None:
    status, answer = store.post("/v1/events", {"events": [{"id": "e9"}, RUN_START]})

    assert status == 200
    assert [refused["index"] for refused in answer["refused"]] == [0]
    assert read_run(store, "t7c1") == [RUN_START]


@pytest.mark.parametrize("document", [b"not json", [], {"events": "none"}])
def test_a_malformed_batch_is_refused_with_the_reason(store: Any, document: Any) -> None:
    status, answer = store.post("/v1/events", document)

    assert status == 400
    assert answer["error"]


def test_an_unknown_version_is_refused_with_the_accepted_versions(store: Any) -> None:
    status, answer = store.post("/v2/events", {"events": [RUN_START]})

    assert status == 404
    assert answer["accepted_versions"] == ["v1"]
