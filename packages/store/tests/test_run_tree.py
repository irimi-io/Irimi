import json
from pathlib import Path
from typing import Any

import pytest

RECORDED_RUN = json.loads(
    (Path(__file__).parents[2] / "sdk/tests/fixtures/recorded_run.json").read_text()
)
TREE_NODE_KEYS = {
    "span_id",
    "parent_span_id",
    "kind",
    "name",
    "seq",
    "start_time_unix_nano",
    "end_time_unix_nano",
    "status",
    "flags",
    "children",
}
CONDITION_CASES = [
    ([{"events_produced": 3}], {}, ["incomplete"]),
    ([{"events_produced": 2}], {"flags": ["not_replayable"]}, ["not_replayable"]),
    ([{"flags": ["not_replayable"]}], {}, ["not_replayable"]),
    ([{"events_produced": "2"}], {"flags": "not_replayable"}, []),
    ([], {}, ["unfinished"]),
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


def test_a_run_is_read_as_a_tree_of_core_fields_only(store: Any) -> None:
    store.post("/v1/events", RECORDED_RUN)

    status, answer = store.get("/api/v1/runs/t7c1")
    run = answer["run"]
    root = run["root"]
    function = root["children"][2]

    assert (status, run["id"], run["conditions"], run["detached"]) == (200, "t7c1", [], [])
    assert root.keys() == function.keys() == TREE_NODE_KEYS
    assert (root["name"], root["status"], root["parent_span_id"]) == ("handle_ticket", "ok", None)
    assert root["start_time_unix_nano"] == "1795341600000000000"
    assert [(child["kind"], child["seq"]) for child in root["children"]] == [
        ("llm", 1),
        ("http", 2),
        ("function", 3),
    ]
    assert (function["end_time_unix_nano"], function["children"], function["flags"]) == (
        "1795341602330000000",
        [],
        [],
    )


def test_a_call_is_read_with_its_bodies(store: Any) -> None:
    store.post("/v1/events", RECORDED_RUN)

    status, answer = store.get("/api/v1/runs/t7c1/calls/s3")

    assert status == 200
    assert answer["call"]["name"] == "POST api.stripe.com/v1/refunds"
    assert answer["call"]["response"]["body"]["json"] == {"id": "re_456", "status": "succeeded"}
    assert answer["call"]["request"]["headers"]["Authorization"] == {
        "$irimi": "redacted",
        "reason": "credential",
    }
    assert (answer["call"]["events"], "children" in answer["call"]) == ([], False)


def test_events_in_any_order_and_repeated_make_the_same_tree(store: Any) -> None:
    events = RECORDED_RUN["events"]
    reversed_twice = [{**event, "trace_id": "t7c2"} for event in [*events[::-1], *events[::-1]]]
    store.post("/v1/events", {"events": events})
    store.post("/v1/events", {"events": reversed_twice})

    ordered = store.get("/api/v1/runs/t7c1")[1]["run"]
    shuffled = store.get("/api/v1/runs/t7c2")[1]["run"]

    assert shuffled == {**ordered, "id": "t7c2"}


def test_gaps_are_shown_not_tidied_away(store: Any) -> None:
    store.post(
        "/v1/events",
        {
            "events": [
                start("t1", "s1", "run", 100),
                start("t1", "s2", "no_end", 110, parent="s1"),
                end("t1", "s3", 130, status="ok"),
                start("t1", "s4", "orphan", 140, parent="never-arrived"),
                start("t1", "s5", "under_orphan", 150, parent="s4"),
                start("t1", "s6", "loops", 160, parent="s7"),
                start("t1", "s7", "loops_back", 170, parent="s6"),
                start("t1", "s8", "own_parent", 180, parent="s8"),
            ]
        },
    )

    run = store.get("/api/v1/runs/t1")[1]["run"]
    no_end = run["root"]["children"][0]
    detached = {node["span_id"]: node for node in run["detached"]}
    ended_only = detached["s3"]

    assert run["conditions"] == ["unfinished"]
    assert [child["span_id"] for child in run["root"]["children"]] == ["s2"]
    assert (no_end["end_time_unix_nano"], no_end["status"]) == (None, None)
    assert (ended_only["kind"], ended_only["name"], ended_only["seq"]) == (None, None, None)
    assert (ended_only["start_time_unix_nano"], ended_only["status"]) == (None, "ok")
    assert [child["span_id"] for child in detached["s4"]["children"]] == ["s5"]
    assert detached.keys() == {"s3", "s4", "s6", "s7", "s8"}


def test_unknown_fields_event_types_and_values_are_kept(store: Any) -> None:
    note = {
        "id": "n1",
        "type": "added_later",
        "trace_id": "t1",
        "span_id": "s1",
        "time_unix_nano": "120",
        "text": "kept",
    }
    second_start = {**start("t1", "s1", "run_again", 105), "id": "retry"}
    store.post(
        "/v1/events",
        {
            "events": [
                start(
                    "t1", "s1", "run", 100, added_later={"a": [1]}, children="not ours", flags="odd"
                ),
                end("t1", "s1", 150, status=7, flags=["unserialisable_output"]),
                note,
                second_start,
            ]
        },
    )

    call = store.get("/api/v1/runs/t1/calls/s1")[1]["call"]

    assert (call["name"], call["added_later"], call["status"]) == ("run", {"a": [1]}, 7)
    assert ("children" in call, call["flags"]) == (False, ["unserialisable_output"])
    assert call["events"] == [note, second_start]


def test_a_second_root_in_one_run_is_detached(store: Any) -> None:
    store.post(
        "/v1/events",
        {"events": [start("t1", "s2", "second", 100, seq=1), start("t1", "s1", "first", 110)]},
    )

    run = store.get("/api/v1/runs/t1")[1]["run"]

    assert run["root"]["name"] == "first"
    assert [node["name"] for node in run["detached"]] == ["second"]


@pytest.mark.parametrize("path", ["/api/v1/runs/missing", "/api/v1/runs/t7c1/calls/missing"])
def test_what_the_store_does_not_hold_is_not_found(store: Any, path: str) -> None:
    store.post("/v1/events", RECORDED_RUN)

    status, answer = store.get(path)

    assert (status, bool(answer["error"])) == (404, True)


def test_a_run_whose_root_never_arrived_says_so_and_is_not_listed(store: Any) -> None:
    store.post("/v1/events", {"events": [start("t1", "s2", "step", 110, parent="s1")]})

    run = store.get("/api/v1/runs/t1")[1]["run"]

    assert (run["conditions"], run["root"]) == (["no_root"], None)
    assert [node["span_id"] for node in run["detached"]] == ["s2"]
    assert store.get("/api/v1/runs")[1]["runs"] == []


def test_a_run_nested_too_deeply_is_refused_as_a_tree_and_still_read_as_events(
    store: Any,
) -> None:
    spans = [f"s{depth}" for depth in range(600)]
    nested = [
        start("t1", span, "step", 100, parent=parent)
        for span, parent in zip(spans, [None, *spans], strict=False)
    ]
    store.post("/v1/events", {"events": nested})

    status, answer = store.get("/api/v1/runs/t1")

    assert (status, "/v1/runs/t1/events" in answer["error"]) == (422, True)
    assert store.get("/v1/runs/t1/events?limit=1")[0] == 200


def test_only_starts_and_ends_count_towards_what_a_run_holds(store: Any) -> None:
    note = {**end("t1", "s1", 120), "id": "n1", "type": "added_later"}
    store.post(
        "/v1/events",
        {"events": [start("t1", "s1", "run", 100), end("t1", "s1", 150, events_produced=3), note]},
    )

    listed = store.get("/api/v1/runs")[1]["runs"][0]["conditions"]
    tree = store.get("/api/v1/runs/t1")[1]["run"]["conditions"]

    assert listed == tree == ["incomplete"]


@pytest.mark.parametrize(("root_ends", "start_fields", "conditions"), CONDITION_CASES)
def test_the_list_and_the_tree_agree_on_conditions(
    store: Any, root_ends: list[dict[str, Any]], start_fields: dict[str, Any], conditions: list[str]
) -> None:
    ends = [end("t1", "s1", 150, **fields) for fields in root_ends]
    store.post("/v1/events", {"events": [start("t1", "s1", "run", 100, **start_fields), *ends]})

    listed = store.get("/api/v1/runs")[1]["runs"][0]["conditions"]
    tree = store.get("/api/v1/runs/t1")[1]["run"]["conditions"]

    assert listed == tree == conditions
