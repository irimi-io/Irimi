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
    "added_by_a_newer_sdk": {"anything": [1, 2]},
}
RUN_END = {
    "id": "e2",
    "type": "end",
    "trace_id": "t7c1",
    "span_id": "s1",
    "time_unix_nano": "1795341602340000000",
    "status": "ok",
}
RUN_NOTE = {
    "id": "e3",
    "type": "added_by_a_newer_sdk",
    "trace_id": "t7c1",
    "span_id": "s1",
    "time_unix_nano": "1795341601000000000",
}
LATE_NOTE = {**RUN_NOTE, "id": "e4"}


def test_a_run_is_read_back_as_written_in_pages_that_hold_while_events_arrive(
    store: Any,
) -> None:
    store.post("/v1/events", {"events": [RUN_END, RUN_START, RUN_NOTE]})
    status, first = store.get("/v1/runs/t7c1/events?limit=2")
    store.post("/v1/events", {"events": [LATE_NOTE, RUN_END]})
    _, second = store.get(f"/v1/runs/t7c1/events?cursor={first['next_cursor']}")

    assert status == 200
    assert first["events"] == [RUN_END, RUN_START]
    assert second == {"events": [RUN_NOTE, LATE_NOTE], "next_cursor": None}


def test_reading_a_run_the_store_does_not_hold_is_not_found(store: Any) -> None:
    status, answer = store.get("/v1/runs/missing/events")

    assert (status, bool(answer["error"])) == (404, True)


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=-1", "limit=many", "limit=1001", "cursor=not-a-cursor"]
)
def test_a_page_request_the_store_cannot_read_is_refused_with_the_reason(
    store: Any, query: str
) -> None:
    store.post("/v1/events", {"events": [RUN_START]})

    status, answer = store.get(f"/v1/runs/t7c1/events?{query}")

    assert (status, bool(answer["error"])) == (400, True)
