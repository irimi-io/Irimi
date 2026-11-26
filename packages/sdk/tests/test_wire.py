import json
from pathlib import Path
from typing import Any

import pytest
from irimi._wire import (
    EndEvent,
    JsonValue,
    StartEvent,
    WireFormatError,
    escape_reserved_keys,
    parse_batch,
    unescape_reserved_keys,
    write_batch,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def batch_of(*events: Any) -> dict[str, Any]:
    return {"events": list(events)}


def start_event(**changes: Any) -> dict[str, Any]:
    event = {
        "id": "e1",
        "type": "start",
        "trace_id": "t1",
        "span_id": "s1",
        "time_unix_nano": "1795341600000000000",
        "parent_span_id": None,
        "kind": "workflow",
        "name": "handle_ticket",
        "seq": 0,
    }
    return {**event, **changes}


def without(event: dict[str, Any], field: str) -> dict[str, Any]:
    return {name: value for name, value in event.items() if name != field}


@pytest.mark.parametrize("fixture", ["recorded_run.json", "replay_run.json"])
def test_a_run_is_written_back_exactly_as_it_was_read(fixture: str) -> None:
    document = load_fixture(fixture)

    parsed = parse_batch(document)

    assert parsed.refused == ()
    assert write_batch(parsed.batch) == document


def test_required_fields_are_typed() -> None:
    events = parse_batch(load_fixture("recorded_run.json")).batch.events
    run_start, run_end = events[0], events[-1]

    assert isinstance(run_start, StartEvent)
    assert (run_start.kind, run_start.name, run_start.seq) == ("workflow", "handle_ticket", 0)
    assert run_start.parent_span_id is None
    assert run_start.time_unix_nano == 1795341600000000000
    assert run_start.fields["environment"] == "production"
    assert isinstance(run_end, EndEvent)
    assert (run_end.trace_id, run_end.span_id) == ("t7c1", "s1")


def test_unknown_types_kinds_and_fields_are_kept() -> None:
    score = {
        "id": "e9",
        "type": "score",
        "trace_id": "t1",
        "span_id": "s1",
        "time_unix_nano": "1795341600000000001",
        "value": 0.4,
    }
    database_call = start_event(kind="database", statement="select 1")
    document = {**batch_of(score, database_call), "region": "eu"}

    batch = parse_batch(document).batch

    assert not isinstance(batch.events[0], (StartEvent, EndEvent))
    assert write_batch(batch) == document


@pytest.mark.parametrize("document", [[], {"sdk": {}}, {"events": {}}])
def test_a_batch_without_a_list_of_events_is_refused(document: Any) -> None:
    with pytest.raises(WireFormatError):
        parse_batch(document)


@pytest.mark.parametrize(
    "event",
    [
        "not an event",
        *(without(start_event(), field) for field in start_event()),
        start_event(id=""),
        start_event(trace_id=7),
        start_event(time_unix_nano=1795341600000000000),
        start_event(time_unix_nano="2026-11-22T10:00:00Z"),
        start_event(time_unix_nano="9" * 5000),
        start_event(time_unix_nano=str(2**63)),
        start_event(seq=2**63),
        start_event(seq="0"),
        start_event(seq=True),
        start_event(parent_span_id=3),
    ],
)
def test_a_malformed_event_is_refused_and_the_rest_of_the_batch_is_kept(event: Any) -> None:
    parsed = parse_batch(batch_of(start_event(), event))

    assert write_batch(parsed.batch) == batch_of(start_event())
    assert [refused.index for refused in parsed.refused] == [1]


def test_real_data_with_the_reserved_key_survives_escaping() -> None:
    marker: JsonValue = {"$irimi": "redacted", "reason": "credential"}
    data: JsonValue = {
        "note": {"$irimi": "redacted", "nested": [{"$irimi": {"$irimi": 1}}]},
        "plain": [1, "a"],
    }

    escaped = escape_reserved_keys(data)

    assert escaped != data
    assert unescape_reserved_keys(escaped) == data
    assert unescape_reserved_keys({"token": marker}) == {"token": marker}
