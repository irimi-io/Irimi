import base64
import gc
import gzip
import io
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import irimi
import pytest
import requests
import urllib3

TakeEvents = Callable[[], list[dict[str, Any]]]
Call = tuple[dict[str, Any], dict[str, Any]]
HALF_A_MEGABYTE = 512 * 1024


def take_calls(recorded_events: TakeEvents) -> list[Call]:
    events = recorded_events()
    ends = {event["span_id"]: event for event in events if event["type"] == "end"}
    return [(event, ends[event["span_id"]]) for event in events if event.get("kind") == "http"]


@pytest.mark.parametrize(
    ("body", "recorded_body"),
    [
        (b"\xff\xfe\x00", {"encoding": "base64", "data": "//4A"}),
        (
            {"note": "café"},
            {
                "encoding": "text",
                "content_type": "application/x-www-form-urlencoded",
                "data": "note=caf%C3%A9",
            },
        ),
    ],
)
def test_a_request_body_is_recorded_as_the_bytes_sent(
    recorded_events: TakeEvents, upstream: Any, body: Any, recorded_body: dict[str, str]
) -> None:
    @irimi.workflow
    def send_note() -> None:
        requests.post(f"{upstream.url}/notes", data=body)

    send_note()

    ((call_start, _),) = take_calls(recorded_events)

    assert call_start["request"]["body"] == recorded_body
    assert "flags" not in call_start


@pytest.mark.parametrize(
    "open_body",
    [lambda: iter([b"ab", b"cd"]), lambda: io.BytesIO(b"abcd")],
    ids=["iterator", "file"],
)
def test_a_streamed_request_body_is_not_recorded(
    recorded_events: TakeEvents, upstream: Any, open_body: Callable[[], Any]
) -> None:
    @irimi.workflow
    def upload_in_parts() -> None:
        requests.post(f"{upstream.url}/uploads", data=open_body())

    upload_in_parts()

    ((call_start, _),) = take_calls(recorded_events)

    assert upstream.requests[-1][2] == b"abcd"
    assert "body" not in call_start["request"]
    assert call_start["flags"] == ["request_body_not_recorded"]


def test_a_response_sent_as_a_request_body_is_not_recorded_as_the_answer(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer("/copies", status=201)

    @irimi.workflow
    def forward_report() -> int:
        report = urllib3.HTTPResponse(body=io.BytesIO(b"report"), status=418, preload_content=False)
        headers = {"Content-Length": "6"}
        return urllib3.request(
            "POST", f"{upstream.url}/copies", body=report, headers=headers
        ).status

    assert forward_report() == 201

    ((call_start, call_end),) = take_calls(recorded_events)

    assert upstream.requests[-1][2] == b"report"
    assert call_start["flags"] == ["request_body_not_recorded"]
    assert call_end["response"]["status"] == 201
    assert call_end["response"]["body"]["data"] == '{"ok": true}'


def test_the_request_headers_recorded_are_the_ones_the_app_set(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    @irimi.workflow
    def look_up_charges() -> None:
        urllib3.request(
            "GET",
            f"{upstream.url}/charges",
            headers={"X-Trace": "t-1", "Accept-Encoding": urllib3.util.SKIP_HEADER},
        )

    look_up_charges()

    ((call_start, _),) = take_calls(recorded_events)

    assert call_start["request"]["headers"] == {"X-Trace": "t-1"}
    assert upstream.request_headers[-1]["X-Trace"] == "t-1"
    assert "Accept-Encoding" not in upstream.request_headers[-1]


def test_a_response_urllib3_reads_before_returning_it_is_recorded_as_it_arrived(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer(
        "/orders",
        headers=[("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
        chunks=[gzip.compress(b'{"ok": true}')],
    )

    @irimi.workflow
    def read_orders() -> Any:
        return urllib3.request("GET", f"{upstream.url}/orders").json()

    assert read_orders() == {"ok": True}

    ((_, call_end),) = take_calls(recorded_events)
    body = call_end["response"]["body"]

    assert (body["encoding"], body["content_encoding"]) == ("base64", "gzip")
    assert gzip.decompress(base64.b64decode(body["data"])) == b'{"ok": true}'
    assert "flags" not in call_end


def test_a_call_sent_through_a_proxy_is_recorded_under_the_address_it_was_meant_for(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    address = "http://api.example.com/v1/charges?limit=3"

    @irimi.workflow
    def look_up_charges() -> int:
        return requests.get(address, proxies={"http": upstream.url}).status_code

    assert look_up_charges() == 200

    ((call_start, _),) = take_calls(recorded_events)

    assert call_start["name"] == "GET api.example.com/v1/charges"
    assert call_start["request"]["url"] == address


def test_a_streamed_response_is_recorded_whole_when_the_stream_ends(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer(
        "/events",
        headers=[("Content-Type", "text/event-stream")],
        chunks=[b"data: one\n\n", b"data: two\n\n"],
    )

    @irimi.workflow
    def follow_events() -> list[bytes]:
        with requests.post(f"{upstream.url}/events", stream=True) as answer:
            return list(answer.iter_lines())

    assert follow_events() == [b"data: one", b"", b"data: two", b""]

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["response"]["body"]["data"] == "data: one\n\ndata: two\n\n"
    assert call_end["status"] == "ok"
    assert "flags" not in call_end


@pytest.mark.parametrize(
    "chunks",
    [[b"x" * HALF_A_MEGABYTE] * 3, [b"x" * 3 * HALF_A_MEGABYTE]],
    ids=["chunked", "content-length"],
)
def test_a_response_closed_early_is_recorded_as_far_as_it_was_read(
    recorded_events: TakeEvents, upstream: Any, chunks: list[bytes]
) -> None:
    upstream.set_answer("/archive", chunks=chunks)

    @irimi.workflow
    def peek_at_archive() -> None:
        with requests.get(f"{upstream.url}/archive", stream=True) as answer:
            next(answer.iter_content(1024))

    peek_at_archive()

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["flags"] == ["response_body_incomplete"]
    assert 0 < len(call_end["response"]["body"]["data"]) < 3 * HALF_A_MEGABYTE


def test_a_response_dropped_unread_is_recorded_when_it_is_collected(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer("/archive", chunks=[b"x" * 3 * HALF_A_MEGABYTE])

    @irimi.workflow
    def peek_at_archive() -> None:
        urllib3.request("GET", f"{upstream.url}/archive", preload_content=False).read(10)

    peek_at_archive()
    gc.collect()

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["flags"] == ["response_body_incomplete"]
    assert call_end["response"]["body"]["data"] == "x" * 10


def test_a_response_whose_connection_the_pool_closed_is_recorded_when_it_is_collected(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer("/archive", chunks=[b"x" * 3 * HALF_A_MEGABYTE])

    @irimi.workflow
    def skip_archive() -> None:
        with urllib3.connection_from_url(upstream.url) as pool:
            archive = pool.request("GET", "/archive", preload_content=False)
            archive.release_conn()

    skip_archive()
    gc.collect()

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["flags"] == ["response_body_incomplete"]


def test_a_read_that_fails_and_leaves_the_response_readable_does_not_end_its_call(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    @irimi.workflow
    def read_order() -> bytes:
        answer = urllib3.request("GET", f"{upstream.url}/orders", preload_content=False)
        with pytest.raises(urllib3.exceptions.ResponseNotChunked):
            next(answer.read_chunked())
        return answer.read()

    assert read_order() == b'{"ok": true}'

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["response"]["body"]["data"] == '{"ok": true}'
    assert "flags" not in call_end


def test_a_response_with_nothing_to_read_ends_without_being_read(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    @irimi.workflow
    def check_refund() -> None:
        answer = urllib3.request("HEAD", f"{upstream.url}/refunds/re_456", preload_content=False)
        answer.release_conn()

    check_refund()

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["status"] == "ok"
    assert "body" not in call_end["response"]
    assert "flags" not in call_end


@pytest.mark.parametrize(
    ("fetch", "raised"),
    [
        (lambda url: requests.get(url), requests.exceptions.ChunkedEncodingError),
        (
            lambda url: urllib3.request("GET", url, retries=False),
            urllib3.exceptions.ProtocolError,
        ),
    ],
    ids=["requests", "urllib3"],
)
def test_a_response_that_breaks_off_is_recorded_with_the_error_urllib3_raised(
    recorded_events: TakeEvents,
    upstream: Any,
    fetch: Callable[[str], Any],
    raised: type[Exception],
) -> None:
    upstream.set_answer("/report", chunks=[b"x" * 1024, b"y"], breaks_off=True)

    @irimi.workflow
    def read_report() -> None:
        fetch(f"{upstream.url}/report")

    with pytest.raises(raised):
        read_report()

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["status"] == "error"
    assert call_end["error"]["type"] == "urllib3.exceptions.ProtocolError"
    assert call_end["response"]["status"] == 200
    assert call_end["flags"] == ["response_body_incomplete"]


def test_a_request_that_cannot_be_sent_is_recorded_with_the_error_urllib3_raised(
    recorded_events: TakeEvents, unused_url: str
) -> None:
    @irimi.workflow
    def reach_nobody() -> None:
        requests.get(f"{unused_url}/charges")

    with pytest.raises(requests.exceptions.ConnectionError):
        reach_nobody()

    ((_, call_end),) = take_calls(recorded_events)

    assert call_end["status"] == "error"
    assert call_end["error"]["type"] == "urllib3.exceptions.NewConnectionError"
    assert "response" not in call_end


def test_each_attempt_of_a_retried_request_is_its_own_call(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer("/busy", status=503, chunks=[b"busy"])
    retries = urllib3.Retry(2, status_forcelist=[503], raise_on_status=False)

    @irimi.workflow
    def keep_trying() -> int:
        return urllib3.request("GET", f"{upstream.url}/busy", retries=retries).status

    assert keep_trying() == 503

    call_ends = [call_end for _, call_end in take_calls(recorded_events)]

    assert [(call_end["status"], call_end["response"]["status"]) for call_end in call_ends] == [
        ("ok", 503)
    ] * 3
    assert {call_end["response"]["body"]["data"] for call_end in call_ends} == {"busy"}


@pytest.mark.parametrize(
    "send",
    [
        lambda url: requests.post(url, json={"amount": 5}).status_code,
        lambda url: urllib3.request("POST", url, json={"amount": 5}).status,
    ],
    ids=["requests", "urllib3"],
)
def test_each_hop_of_a_followed_redirect_is_recorded_with_its_body(
    recorded_events: TakeEvents, upstream: Any, send: Callable[[str], int]
) -> None:
    upstream.set_answer("/v1/refunds", status=307, headers=[("Location", "/v2/refunds")])

    @irimi.workflow
    def send_refund() -> int:
        return send(f"{upstream.url}/v1/refunds")

    assert send_refund() == 200

    (first_start, first_end), (second_start, _) = take_calls(recorded_events)

    assert (first_start["name"], second_start["name"]) == (
        "POST 127.0.0.1/v1/refunds",
        "POST 127.0.0.1/v2/refunds",
    )
    assert first_end["response"]["status"] == 307
    for call_start in (first_start, second_start):
        assert "flags" not in call_start
        assert json.loads(call_start["request"]["body"]["data"]) == {"amount": 5}


def test_calls_made_at_once_on_several_threads_each_end_on_their_own_call(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    for number in range(8):
        upstream.set_answer(f"/orders/{number}", headers=[("X-Order", str(number))])

    def read_order(number: int) -> int:
        return urllib3.request("GET", f"{upstream.url}/orders/{number}").status

    with ThreadPoolExecutor(max_workers=8) as threads:
        assert list(threads.map(read_order, range(8))) == [200] * 8

    calls = take_calls(recorded_events)

    assert len(calls) == 8
    for call_start, call_end in calls:
        order = call_end["response"]["headers"]["X-Order"]
        assert call_start["request"]["url"] == f"{upstream.url}/orders/{order}"
