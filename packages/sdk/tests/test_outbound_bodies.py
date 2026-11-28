import asyncio
import base64
import gzip
from collections.abc import Callable
from typing import Any

import irimi
import pytest

TakeEvents = Callable[[], list[dict[str, Any]]]
LIMIT = 4 * 1024 * 1024
HALF_A_MEGABYTE = 512 * 1024


def take_call(recorded_events: TakeEvents) -> tuple[dict[str, Any], dict[str, Any]]:
    events = recorded_events()
    (call_start,) = [event for event in events if event.get("kind") == "http"]
    (call_end,) = [
        event
        for event in events
        if event["type"] == "end" and event["span_id"] == call_start["span_id"]
    ]
    return call_start, call_end


def test_a_compressed_response_is_recorded_as_it_arrived(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer(
        "/orders",
        headers=[("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
        chunks=[gzip.compress(b'{"ok": true}')],
    )

    @irimi.workflow
    def read_orders() -> Any:
        with http_library.Client() as client:
            return client.get(f"{upstream.url}/orders").json()

    assert read_orders() == {"ok": True}

    _, call_end = take_call(recorded_events)
    body = call_end["response"]["body"]

    assert (body["encoding"], body["content_encoding"]) == ("base64", "gzip")
    assert gzip.decompress(base64.b64decode(body["data"])) == b'{"ok": true}'
    assert call_end["response"]["headers"]["Content-Encoding"] == "gzip"


def test_a_binary_body_is_recorded_as_base64(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    @irimi.workflow
    def upload_file() -> None:
        with http_library.Client() as client:
            client.post(
                f"{upstream.url}/files",
                content=b"\xff\xfe\x00",
                headers={"Content-Type": "application/octet-stream"},
            )

    upload_file()

    call_start, _ = take_call(recorded_events)

    assert call_start["request"]["body"] == {
        "encoding": "base64",
        "content_type": "application/octet-stream",
        "data": "//4A",
    }


@pytest.mark.parametrize(
    ("content", "recorded_length"),
    [(b"x" * (LIMIT + 10), LIMIT), (b"a" + "é".encode() * (LIMIT // 2), LIMIT - 1)],
)
def test_a_response_larger_than_the_limit_is_cut_and_flagged(
    recorded_events: TakeEvents,
    upstream: Any,
    http_library: Any,
    content: bytes,
    recorded_length: int,
) -> None:
    upstream.set_answer("/export", headers=[("Content-Type", "text/plain")], chunks=[content])

    @irimi.workflow
    def download_export() -> bytes:
        with http_library.Client() as client:
            return bytes(client.get(f"{upstream.url}/export").content)

    assert download_export() == content

    _, call_end = take_call(recorded_events)
    body = call_end["response"]["body"]

    assert body["encoding"] == "text"
    assert len(body["data"].encode()) == recorded_length
    assert call_end["flags"] == ["response_body_truncated"]


def test_a_request_larger_than_the_limit_is_cut_and_flagged(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    @irimi.workflow
    def upload_export() -> None:
        with http_library.Client() as client:
            client.post(f"{upstream.url}/imports", content=b"x" * (LIMIT + 10))

    upload_export()

    call_start, _ = take_call(recorded_events)

    assert len(upstream.requests[-1][2]) == LIMIT + 10
    assert len(call_start["request"]["body"]["data"]) == LIMIT
    assert call_start["flags"] == ["request_body_truncated"]


def test_a_streamed_request_body_is_not_recorded(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    @irimi.workflow
    def upload_in_parts() -> None:
        with http_library.Client() as client:
            client.post(f"{upstream.url}/uploads", content=iter([b"ab", b"cd"]))

    upload_in_parts()

    call_start, _ = take_call(recorded_events)

    assert upstream.requests[-1][2] == b"abcd"
    assert "body" not in call_start["request"]
    assert call_start["flags"] == ["request_body_not_recorded"]


@pytest.mark.parametrize(
    ("status", "method", "refund", "recorded_body"),
    [
        (302, "GET", None, None),
        (
            307,
            "POST",
            {"amount": 5},
            {"encoding": "text", "content_type": "application/json", "data": '{"amount":5}'},
        ),
    ],
)
def test_each_hop_of_a_followed_redirect_is_recorded_with_its_body(
    recorded_events: TakeEvents,
    upstream: Any,
    http_library: Any,
    status: int,
    method: str,
    refund: dict[str, int] | None,
    recorded_body: dict[str, str] | None,
) -> None:
    upstream.set_answer("/v1/refunds", status=status, headers=[("Location", "/v2/refunds")])

    @irimi.workflow
    def send_refund() -> int:
        with http_library.Client(follow_redirects=True) as client:
            return int(
                client.request(method, f"{upstream.url}/v1/refunds", json=refund).status_code
            )

    assert send_refund() == 200

    starts = [event for event in recorded_events() if event.get("kind") == "http"]

    assert [start["name"] for start in starts] == [
        f"{method} 127.0.0.1/v1/refunds",
        f"{method} 127.0.0.1/v2/refunds",
    ]
    for start in starts:
        assert "flags" not in start
        assert start["request"].get("body") == recorded_body
    assert upstream.requests[-1] == (
        method,
        "/v2/refunds",
        b'{"amount":5}' if refund is not None else b"",
    )


def test_a_streamed_response_is_recorded_whole_when_the_stream_ends(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer(
        "/events",
        headers=[("Content-Type", "text/event-stream")],
        chunks=[b"data: one\n\n", b"data: two\n\n"],
    )

    @irimi.workflow
    def follow_events() -> list[str]:
        with (
            http_library.Client() as client,
            client.stream("POST", f"{upstream.url}/events") as answer,
        ):
            return list(answer.iter_lines())

    assert follow_events() == ["data: one", "", "data: two", ""]

    _, call_end = take_call(recorded_events)

    assert call_end["response"]["body"]["data"] == "data: one\n\ndata: two\n\n"
    assert call_end["status"] == "ok"
    assert "flags" not in call_end


def test_an_event_stream_closed_after_an_event_is_recorded_as_whole(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer(
        "/completions",
        headers=[("Content-Type", "text/event-stream; charset=utf-8")],
        chunks=[b"data: one\n\ndata: [DONE]\n\n", b"data: never read\n\n"],
    )

    @irimi.workflow
    def follow_completion() -> None:
        with (
            http_library.Client() as client,
            client.stream("POST", f"{upstream.url}/completions") as answer,
        ):
            next(line for line in answer.iter_lines() if line == "data: [DONE]")

    follow_completion()

    _, call_end = take_call(recorded_events)

    assert call_end["response"]["body"]["data"].startswith("data: one\n\ndata: [DONE]\n\n")
    assert "flags" not in call_end


def test_a_text_response_that_stops_inside_a_character_is_recorded_as_text(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/menu", chunks=[b"caf\xc3"], breaks_off=True)

    @irimi.workflow
    def read_menu() -> None:
        with http_library.Client() as client:
            client.get(f"{upstream.url}/menu")

    with pytest.raises(http_library.RemoteProtocolError):
        read_menu()

    _, call_end = take_call(recorded_events)

    assert (call_end["response"]["body"]["encoding"], call_end["response"]["body"]["data"]) == (
        "text",
        "caf",
    )
    assert call_end["flags"] == ["response_body_incomplete"]


def test_a_response_closed_early_is_recorded_as_far_as_it_was_read(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/archive", chunks=[b"x" * HALF_A_MEGABYTE] * 3)

    @irimi.workflow
    def peek_at_archive() -> None:
        with (
            http_library.Client() as client,
            client.stream("GET", f"{upstream.url}/archive") as answer,
        ):
            next(answer.iter_raw())

    peek_at_archive()

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["flags"] == ["response_body_incomplete"]
    assert 0 < len(call_end["response"]["body"]["data"]) < 3 * HALF_A_MEGABYTE


def test_an_async_response_closed_early_is_recorded_as_far_as_it_was_read(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/archive", chunks=[b"x" * HALF_A_MEGABYTE] * 3)

    @irimi.workflow
    async def peek_at_archive() -> None:
        async with (
            http_library.AsyncClient() as client,
            client.stream("GET", f"{upstream.url}/archive") as answer,
        ):
            await anext(answer.aiter_raw())

    asyncio.run(peek_at_archive())

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["flags"] == ["response_body_incomplete"]
    assert 0 < len(call_end["response"]["body"]["data"]) < 3 * HALF_A_MEGABYTE


def test_a_response_that_breaks_off_is_recorded_with_its_error(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/report", chunks=[b"x" * 1024, b"y"], breaks_off=True)

    @irimi.workflow
    def read_report() -> None:
        with http_library.Client() as client:
            client.get(f"{upstream.url}/report")

    with pytest.raises(http_library.RemoteProtocolError):
        read_report()

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "error"
    assert call_end["error"]["type"].endswith("RemoteProtocolError")
    assert call_end["response"]["status"] == 200
    assert call_end["flags"] == ["response_body_incomplete"]


def test_an_async_response_that_breaks_off_is_recorded_with_its_error(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/report", chunks=[b"x" * 1024], breaks_off=True)

    @irimi.workflow
    async def read_report() -> None:
        async with http_library.AsyncClient() as client:
            await client.get(f"{upstream.url}/report")

    with pytest.raises(http_library.RemoteProtocolError):
        asyncio.run(read_report())

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "error"
    assert call_end["error"]["type"].endswith("RemoteProtocolError")
    assert call_end["response"]["status"] == 200
    assert call_end["flags"] == ["response_body_incomplete"]


def test_a_request_that_cannot_be_sent_is_recorded_and_raised_unchanged(
    recorded_events: TakeEvents, unused_url: str, http_library: Any
) -> None:
    @irimi.workflow
    def reach_nobody() -> None:
        with http_library.Client() as client:
            client.get(f"{unused_url}/charges")

    with pytest.raises(http_library.ConnectError):
        reach_nobody()

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "error"
    assert call_end["error"]["type"].endswith("ConnectError")
    assert "response" not in call_end


@pytest.mark.parametrize(("method", "status"), [("DELETE", 204), ("HEAD", 200)])
def test_a_response_with_no_body_has_no_body_recorded(
    recorded_events: TakeEvents, upstream: Any, http_library: Any, method: str, status: int
) -> None:
    upstream.set_answer("/refunds/re_456", status=status, chunks=[])

    @irimi.workflow
    def touch_refund() -> int:
        with http_library.Client() as client:
            return int(client.request(method, f"{upstream.url}/refunds/re_456").status_code)

    assert touch_refund() == status

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["response"]["status"] == status
    assert "body" not in call_end["response"]


def test_a_response_read_more_than_once_is_recorded_once(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    @irimi.workflow
    def read_order_again_and_again() -> str:
        with http_library.Client() as client:
            answer = client.get(f"{upstream.url}/orders/or_1")
            answer.read()
            answer.read()
            return f"{answer.text}{answer.content.decode()}"

    assert read_order_again_and_again() == '{"ok": true}{"ok": true}'

    _, call_end = take_call(recorded_events)

    assert call_end["response"]["body"]["data"] == '{"ok": true}'
    assert "flags" not in call_end


def test_many_streams_open_at_once_each_end_on_their_own_call(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    contents = {
        f"/s/{number}": [f"{number}-a".encode(), f"{number}-b".encode()] for number in range(20)
    }
    for path, chunks in contents.items():
        upstream.set_answer(path, chunks=chunks)

    async def read_stream(client: Any, path: str) -> bytes:
        async with client.stream("GET", f"{upstream.url}{path}") as answer:
            return b"".join([chunk async for chunk in answer.aiter_bytes()])

    @irimi.workflow
    async def read_all_streams() -> list[bytes]:
        async with http_library.AsyncClient() as client:
            return await asyncio.gather(*(read_stream(client, path) for path in contents))

    assert asyncio.run(read_all_streams()) == [b"".join(chunks) for chunks in contents.values()]

    events = recorded_events()
    run_start = events[0]
    starts = {event["span_id"]: event for event in events if event.get("kind") == "http"}
    ends = [event for event in events if event["type"] == "end" and event["span_id"] in starts]

    assert (len(starts), len(ends)) == (20, 20)
    assert {end["span_id"] for end in ends} == set(starts)
    assert {start["parent_span_id"] for start in starts.values()} == {run_start["span_id"]}
    for end in ends:
        path = starts[end["span_id"]]["request"]["url"].removeprefix(upstream.url)
        assert end["response"]["body"]["data"] == b"".join(contents[path]).decode()
