import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from irimi._recorded_calls import (
    CallRole,
    NotReplayableError,
    RecordedCall,
    RecordedOutput,
    RecordedResponse,
    read_recorded_run,
)
from irimi._replay_decisions import (
    FAKED_UNDESCRIBED_CALL,
    CandidateCall,
    Decision,
    ReplayDecider,
)
from irimi._replay_fields import describe_decision
from irimi._wire import Event, JsonValue, parse_event

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_events(name: str) -> list[Event]:
    document = json.loads((FIXTURES / name).read_text())
    return [parse_event(event) for event in document["events"]]


def start(span_id: str, seq: int, kind: str, name: str, parent: str | None, **fields: Any) -> Event:
    return parse_event(
        {
            "id": f"start-{span_id}",
            "type": "start",
            "trace_id": "t1",
            "span_id": span_id,
            "time_unix_nano": "1795341600000000000",
            "parent_span_id": parent,
            "kind": kind,
            "name": name,
            "seq": seq,
            **fields,
        }
    )


def end(span_id: str, **fields: Any) -> Event:
    return parse_event(
        {
            "id": f"end-{span_id}",
            "type": "end",
            "trace_id": "t1",
            "span_id": span_id,
            "time_unix_nano": "1795341600000000001",
            "status": "ok",
            **fields,
        }
    )


def root() -> Event:
    return start("root", 0, "workflow", "handle_ticket", None, input={"args": [], "kwargs": {}})


def http_start(
    span_id: str, method: str, body: dict[str, Any] | None, headers: dict[str, Any], seq: int = 1
) -> Event:
    request: dict[str, Any] = {"method": method, "url": "https://example.com/x", "headers": headers}
    if body is not None:
        request["body"] = body
    return start(span_id, seq, "http", f"{method} example.com/x", "root", request=request)


def json_body(text: str) -> dict[str, Any]:
    return {"encoding": "text", "data": text, "content_type": "application/json"}


def test_a_recorded_run_lists_its_calls_in_order_with_roles() -> None:
    run = read_recorded_run(load_fixture_events("recorded_run.json"))

    assert [(call.name, call.role) for call in run.calls] == [
        ("anthropic messages.create", "llm"),
        ("POST api.stripe.com/v1/refunds", "http_write"),
        ("record_refund", "write_function"),
    ]
    assert run.trace_id == "t7c1"
    assert run.root.name == "handle_ticket"
    assert run.calls[2].result == RecordedOutput({"row_id": 9917})


@pytest.mark.parametrize(
    "end_fields",
    [
        None,
        {"status": "error"},
        {"status": "ok", "flags": ["response_body_truncated"]},
        {"status": "ok", "flags": ["response_body_incomplete"]},
    ],
)
def test_an_http_call_without_a_whole_response_cannot_be_served(
    end_fields: dict[str, Any] | None,
) -> None:
    events = [root(), http_start("call", "GET", None, {})]
    if end_fields is not None:
        events.append(end("call", response={"status": 200, "headers": {}}, **end_fields))

    assert read_recorded_run(events).calls[0].result is None


def test_a_function_whose_output_was_not_whole_cannot_be_served() -> None:
    events = [
        root(),
        start("call", 1, "function", "read_row", "root", input={"args": [], "kwargs": {}}),
        end("call", output=None, flags=["unserialisable_output"]),
    ]

    assert read_recorded_run(events).calls[0].result is None


def test_calls_inside_a_write_function_are_left_out() -> None:
    events = [
        root(),
        start("write", 1, "function", "save", "root", write=True, input={"args": [], "kwargs": {}}),
        start("inner", 2, "function", "inner", "write", input={"args": [], "kwargs": {}}),
        start("deep", 3, "http", "GET example.com/x", "inner", request={"method": "GET"}),
        start("after", 4, "function", "after", "root", input={"args": [], "kwargs": {}}),
    ]

    run = read_recorded_run(events)

    assert [call.name for call in run.calls] == ["save", "after"]


def test_a_run_flagged_not_replayable_is_refused() -> None:
    flagged = start("root", 0, "workflow", "handle_ticket", None, flags=["not_replayable"])

    with pytest.raises(NotReplayableError):
        read_recorded_run([flagged])


def test_a_run_without_a_root_is_refused() -> None:
    with pytest.raises(NotReplayableError):
        read_recorded_run([http_start("call", "GET", None, {})])


def test_a_json_body_is_keyed_by_value_and_headers_are_ignored() -> None:
    events = [
        root(),
        http_start("a", "POST", json_body('{"a": 1, "b": 2}'), {"x-trace": "one"}, seq=1),
        http_start("b", "POST", json_body('{"b":2,"a":1}'), {"x-trace": "two"}, seq=2),
        http_start("c", "POST", {"encoding": "base64", "data": "AAEC"}, {}, seq=3),
        http_start("d", "POST", None, {}, seq=4),
    ]

    keys = [call.match_key for call in read_recorded_run(events).calls]

    assert keys[0] == keys[1] == ["POST", "https://example.com/x", {"a": 1, "b": 2}]
    assert keys[2] == ["POST", "https://example.com/x", ["base64", "AAEC"]]
    assert keys[3] == ["POST", "https://example.com/x", None]


def test_a_text_body_that_is_not_json_is_keyed_by_its_text() -> None:
    body = {"encoding": "text", "data": "plain words", "content_type": "text/plain"}
    events = [root(), http_start("call", "POST", body, {})]

    key = read_recorded_run(events).calls[0].match_key

    assert key == ["POST", "https://example.com/x", ["text", "plain words"]]


def test_a_response_body_that_does_not_decode_cannot_be_served() -> None:
    events = [
        root(),
        http_start("call", "GET", None, {}),
        end(
            "call",
            response={"status": 200, "headers": {}, "body": {"encoding": "base64", "data": "abc"}},
        ),
    ]

    assert read_recorded_run(events).calls[0].result is None


@pytest.mark.parametrize(
    ("kind", "fields", "expected_role"),
    [
        ("http", {"request": {"method": "GET"}}, "http_read"),
        ("http", {"request": {"method": "HEAD"}}, "http_read"),
        ("http", {"request": {"method": "OPTIONS"}}, "http_read"),
        ("http", {"request": {"method": "POST"}}, "http_write"),
        ("http", {"request": {"method": "DELETE"}}, "http_write"),
        ("function", {"read": True}, "read_function"),
        ("function", {"write": True}, "write_function"),
        ("function", {}, "plain"),
        ("mystery", {}, "plain"),
    ],
)
def test_each_recorded_call_gets_a_role_from_its_kind_and_flags(
    kind: str, fields: dict[str, Any], expected_role: str
) -> None:
    events = [root(), start("call", 1, kind, "call", "root", **fields)]

    assert read_recorded_run(events).calls[0].role == expected_role


def test_a_function_is_keyed_by_its_input_without_unserialisable_markers() -> None:
    session = {"$irimi": "unserialisable", "type": "Session"}
    events = [
        root(),
        start("call", 1, "function", "save", "root", input={"args": [session, 1], "kwargs": {}}),
    ]

    assert read_recorded_run(events).calls[0].match_key == {"args": [None, 1], "kwargs": {}}


def test_a_served_response_drops_transfer_encoding_and_fixes_its_length() -> None:
    body = b"\xff\x00hello"
    events = [
        root(),
        http_start("call", "GET", None, {}),
        end(
            "call",
            response={
                "status": 201,
                "headers": {
                    "Transfer-Encoding": "chunked",
                    "Content-Length": "999",
                    "Content-Encoding": "gzip",
                    "Content-Type": "application/octet-stream",
                    "Set-Cookie": {"$irimi": "redacted", "reason": "credential"},
                },
                "body": {"encoding": "base64", "data": base64.b64encode(body).decode()},
            },
        ),
    ]

    result = read_recorded_run(events).calls[0].result

    assert result == RecordedResponse(
        201,
        (("Content-Type", "application/octet-stream"), ("content-length", str(len(body)))),
        body,
    )


def test_content_encoding_is_kept_when_the_body_description_names_it() -> None:
    events = [
        root(),
        http_start("call", "GET", None, {}),
        end(
            "call",
            response={
                "status": 200,
                "headers": {"content-encoding": "gzip"},
                "body": {"encoding": "text", "data": "x", "content_encoding": "gzip"},
            },
        ),
    ]

    result = read_recorded_run(events).calls[0].result

    assert isinstance(result, RecordedResponse)
    assert ("content-encoding", "gzip") in result.headers


SERVED_OUTPUT = RecordedOutput(1)


def recorded(
    span_id: str,
    role: CallRole,
    name: str = "call",
    key: JsonValue = "key",
    result: RecordedResponse | RecordedOutput | None = SERVED_OUTPUT,
) -> RecordedCall:
    return RecordedCall(span_id, name, int(span_id[1:]), role, key, result)


def candidate(role: CallRole, key: JsonValue = "key", name: str = "call") -> CandidateCall:
    return CandidateCall(role, name, key)


@pytest.mark.parametrize(
    ("role", "key", "expected"),
    [
        ("plain", "other", ("live", "s1", "same kind and name", False, ())),
        ("llm", "key", ("recorded", "s1", "same request", False, ())),
        ("llm", "other", ("live", "s1", "same kind and name", False, ())),
        ("http_read", "key", ("recorded", "s1", "same request", False, ())),
        ("http_read", "other", ("live", None, None, False, ("unmatched_read",))),
        ("read_function", "key", ("recorded", "s1", "same request", False, ())),
        ("read_function", "other", ("live", None, None, False, ("unmatched_read",))),
        ("http_write", "key", ("recorded", "s1", "same request", False, ())),
        ("http_write", "other", ("recorded", "s1", "same name, same position", True, ())),
        ("write_function", "key", ("recorded", "s1", "same request", False, ())),
        ("write_function", "other", ("recorded", "s1", "same name, same position", True, ())),
    ],
)
def test_a_call_with_a_recording_is_answered_by_its_role(
    role: CallRole, key: str, expected: tuple[Any, ...]
) -> None:
    decision = ReplayDecider((recorded("s1", role),)).decide(candidate(role, key))

    answer, span_id, rule, is_changed, flags = expected
    assert decision.answer == answer
    assert (decision.matched.span_id if decision.matched else None) == span_id
    assert (decision.rule, decision.is_changed, decision.flags) == (rule, is_changed, flags)
    assert decision.role == role


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("plain", ("live", ())),
        ("llm", ("live", ())),
        ("http_read", ("live", ("unmatched_read",))),
        ("read_function", ("live", ("unmatched_read",))),
        ("http_write", ("faked", ("new_write",))),
        ("write_function", ("faked", ("new_write",))),
    ],
)
def test_a_call_with_no_recording_is_answered_by_its_role(
    role: CallRole, expected: tuple[str, tuple[str, ...]]
) -> None:
    decision = ReplayDecider(()).decide(candidate(role))

    assert (decision.answer, decision.flags) == expected
    assert decision.matched is None
    assert decision.rule is None
    assert not decision.is_changed


def test_identical_reads_are_answered_in_recorded_order() -> None:
    decider = ReplayDecider(
        (
            recorded("s1", "http_read", key="charge"),
            recorded("s2", "http_write", key="refund"),
            recorded("s3", "http_read", key="charge"),
        )
    )

    decisions = [
        decider.decide(candidate("http_read", "charge")),
        decider.decide(candidate("http_write", "refund")),
        decider.decide(candidate("http_read", "charge")),
    ]

    assert [d.answer for d in decisions] == ["recorded", "recorded", "recorded"]
    assert [d.matched.span_id for d in decisions if d.matched] == ["s1", "s2", "s3"]


def test_a_third_identical_read_goes_live_and_is_flagged() -> None:
    decider = ReplayDecider((recorded("s1", "http_read"), recorded("s2", "http_read")))
    decisions = [decider.decide(candidate("http_read")) for _ in range(3)]

    assert [d.answer for d in decisions] == ["recorded", "recorded", "live"]
    assert decisions[2].flags == ("unmatched_read",)
    assert decisions[2].matched is None


@pytest.mark.parametrize(("key", "is_changed"), [("key", False), ("other", True)])
def test_a_write_whose_recorded_value_cannot_be_served_is_paired_and_faked(
    key: str, is_changed: bool
) -> None:
    unservable = recorded("s1", "http_write", result=None)

    decision = ReplayDecider((unservable,)).decide(candidate("http_write", key))

    assert decision.answer == "faked"
    assert decision.matched == unservable
    assert decision.flags == ("recorded_value_unservable",)
    assert decision.is_changed is is_changed


@pytest.mark.parametrize("role", ["llm", "http_read", "read_function"])
def test_a_read_whose_recorded_value_cannot_be_served_goes_live_but_stays_paired(
    role: CallRole,
) -> None:
    unservable = recorded("s1", role, result=None)

    decision = ReplayDecider((unservable,)).decide(candidate(role))

    assert decision.answer == "live"
    assert decision.matched == unservable
    assert decision.rule == "same request"
    assert decision.flags == ("recorded_value_unservable",)


def test_each_recorded_call_is_served_once_under_concurrent_callers() -> None:
    decider = ReplayDecider((recorded("s1", "http_read"), recorded("s2", "http_read")))
    barrier = threading.Barrier(8)

    def decide_once() -> Decision:
        barrier.wait()
        return decider.decide(candidate("http_read"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = [future.result() for future in [pool.submit(decide_once) for _ in range(8)]]

    served = [d.matched.span_id for d in decisions if d.answer == "recorded" and d.matched]
    assert sorted(served) == ["s1", "s2"]
    assert sum(d.answer == "live" for d in decisions) == 6


def test_a_decision_is_written_as_wire_fields() -> None:
    decision = Decision(
        "http_write", "recorded", recorded("s4", "http_write"), "same name, same position", True, ()
    )

    assert describe_decision(decision) == {
        "answered": "recorded",
        "matched": {"span_id": "s4", "rule": "same name, same position"},
        "changed": True,
    }


def test_an_unmatched_decision_is_written_with_only_its_flags() -> None:
    assert describe_decision(FAKED_UNDESCRIBED_CALL) == {
        "answered": "faked",
        "flags": ["undescribed_call"],
    }


def test_an_unmatched_read_leaves_the_recording_unclaimed() -> None:
    decider = ReplayDecider((recorded("s1", "http_read", key="charge"),))

    first = decider.decide(candidate("http_read", "other"))
    second = decider.decide(candidate("http_read", "charge"))

    assert first.answer == "live"
    assert second.answer == "recorded"
    assert second.matched is not None
    assert second.matched.span_id == "s1"


def test_a_claimed_recorded_write_is_not_paired_again() -> None:
    decider = ReplayDecider((recorded("s1", "http_write"),))

    first = decider.decide(candidate("http_write"))
    second = decider.decide(candidate("http_write"))

    assert first.answer == "recorded"
    assert second.answer == "faked"
    assert second.matched is None
    assert second.flags == ("new_write",)
