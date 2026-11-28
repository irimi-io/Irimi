import asyncio
import base64
import contextlib
import gzip
import io
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp
import irimi
import pytest
from aiohttp import web
from irimi._wire import parse_batch

TakeEvents = Callable[[], list[dict[str, Any]]]
Handler = Callable[[aiohttp.ClientRequest], Awaitable[aiohttp.ClientResponse]]
REDACTED = {"$irimi": "redacted", "reason": "credential"}
HALF_A_MEGABYTE = 512 * 1024
EVENTS = [b"data: one\n\n", b"data: two\n\n"]
ALL_EVENTS = b"".join(EVENTS)
PARTS = (
    b"--b\r\nContent-Type: text/plain\r\n\r\n"
    + b"A" * 300_000
    + b"\r\n--b\r\nContent-Type: text/plain\r\n\r\n"
    + b"B" * 200_000
    + b"\r\n--b--\r\n"
)


def take_call(recorded_events: TakeEvents) -> tuple[dict[str, Any], dict[str, Any]]:
    events = recorded_events()
    (call_start,) = [event for event in events if event.get("kind") == "http"]
    (call_end,) = [
        event
        for event in events
        if event["type"] == "end" and event["span_id"] == call_start["span_id"]
    ]
    return call_start, call_end


@contextlib.asynccontextmanager
async def serve(path: str, handler: Callable[[web.Request], Awaitable[Any]]) -> AsyncIterator[str]:
    application = web.Application()
    application.router.add_get(path, handler)
    runner = web.AppRunner(application)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def test_a_call_through_aiohttp_is_recorded_as_a_step_of_its_run(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer("/v1/refunds", chunks=[b'{"id": "re_456"}'])

    @irimi.workflow
    async def refund_ticket(amount: int) -> str:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{upstream.url}/v1/refunds", params={"expand": "charge"}, json={"amount": amount}
            ) as answer,
        ):
            refund = await answer.json()
        return str(refund["id"])

    assert asyncio.run(refund_ticket(5)) == "re_456"

    run_start, call_start, call_end, run_end = recorded_events()

    assert parse_batch({"events": [call_start, call_end]}).refused == ()
    assert (call_start["kind"], call_start["name"], call_start["seq"]) == (
        "http",
        "POST 127.0.0.1/v1/refunds",
        1,
    )
    assert call_start["parent_span_id"] == run_start["span_id"]
    assert call_start["request"]["method"] == "POST"
    assert call_start["request"]["url"] == f"{upstream.url}/v1/refunds?expand=charge"
    assert call_start["request"]["body"] == {
        "encoding": "text",
        "content_type": "application/json",
        "data": '{"amount": 5}',
    }
    assert (call_end["span_id"], call_end["status"], call_end["answered"]) == (
        call_start["span_id"],
        "ok",
        "live",
    )
    assert call_end["response"]["status"] == 200
    assert call_end["response"]["headers"]["Content-Type"] == "application/json"
    assert call_end["response"]["body"]["data"] == '{"id": "re_456"}'
    assert run_end["events_produced"] == 4


def test_what_the_apps_own_middlewares_add_is_recorded(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    async def add_tenant(
        request: aiohttp.ClientRequest, handler: Handler
    ) -> aiohttp.ClientResponse:
        request.headers["X-Tenant"] = "acme"
        return await handler(request)

    @irimi.workflow
    async def look_up_both() -> None:
        async with aiohttp.ClientSession(middlewares=(add_tenant,)) as session:
            async with session.get(f"{upstream.url}/a") as answer:
                await answer.read()
            async with session.get(f"{upstream.url}/b", middlewares=()) as answer:
                await answer.read()

    asyncio.run(look_up_both())

    first_start, second_start = [
        event for event in recorded_events() if event.get("kind") == "http"
    ]
    first_sent, second_sent = upstream.request_headers

    assert (first_start["name"], second_start["name"]) == ("GET 127.0.0.1/a", "GET 127.0.0.1/b")
    assert first_start["request"]["headers"]["X-Tenant"] == "acme"
    assert "X-Tenant" not in second_start["request"]["headers"]
    assert first_sent["X-Tenant"] == "acme"
    assert "X-Tenant" not in second_sent


async def read_whole(answer: aiohttp.ClientResponse) -> bytes:
    return await answer.read()


async def read_by_line(answer: aiohttp.ClientResponse) -> bytes:
    return b"".join([line async for line in answer.content])


async def read_in_sevens(answer: aiohttp.ClientResponse) -> bytes:
    return b"".join([chunk async for chunk in answer.content.iter_chunked(7)])


async def read_as_it_arrives(answer: aiohttp.ClientResponse) -> bytes:
    return b"".join([chunk async for chunk in answer.content.iter_any()])


async def read_by_http_chunk(answer: aiohttp.ClientResponse) -> bytes:
    return b"".join([chunk async for chunk, _ in answer.content.iter_chunks()])


async def read_exact_counts(answer: aiohttp.ClientResponse) -> bytes:
    counted = await answer.content.readexactly(5) + await answer.content.readexactly(17)
    return counted + await answer.content.read()


async def read_by_event(answer: aiohttp.ClientResponse) -> bytes:
    events = await answer.content.readuntil(b"\n\n") + await answer.content.readuntil(b"\n\n")
    return events + await answer.content.read()


async def read_past_the_end(answer: aiohttp.ClientResponse) -> bytes:
    with pytest.raises(asyncio.IncompleteReadError) as raised:
        await answer.content.readexactly(len(ALL_EVENTS) + 1)
    return raised.value.partial


async def read_once_it_has_all_arrived(answer: aiohttp.ClientResponse) -> bytes:
    await answer.content.wait_eof()
    return answer.content.read_nowait()


@pytest.mark.parametrize(
    "read_answer",
    [
        read_whole,
        read_by_line,
        read_in_sevens,
        read_as_it_arrives,
        read_by_http_chunk,
        read_exact_counts,
        read_by_event,
        read_past_the_end,
        read_once_it_has_all_arrived,
    ],
)
def test_a_body_is_recorded_whole_however_the_app_reads_it(
    recorded_events: TakeEvents,
    upstream: Any,
    read_answer: Callable[[aiohttp.ClientResponse], Awaitable[bytes]],
) -> None:
    upstream.set_answer("/events", headers=[("Content-Type", "text/event-stream")], chunks=EVENTS)

    @irimi.workflow
    async def follow_events() -> tuple[bytes, str]:
        async with (
            aiohttp.ClientSession() as session,
            session.post(f"{upstream.url}/events") as answer,
        ):
            return await read_answer(answer), repr(answer.content)

    content, description = asyncio.run(follow_events())

    _, call_end = take_call(recorded_events)

    assert content == ALL_EVENTS
    assert description.startswith("<StreamReader")
    assert call_end["response"]["body"]["data"] == ALL_EVENTS.decode()
    assert call_end["status"] == "ok"
    assert "flags" not in call_end


@pytest.mark.parametrize("is_decoded", [True, False])
def test_a_compressed_response_is_recorded_as_the_app_read_it(
    recorded_events: TakeEvents, upstream: Any, is_decoded: bool
) -> None:
    compressed = gzip.compress(b'{"ok": true}')
    upstream.set_answer(
        "/orders",
        headers=[("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
        chunks=[compressed],
    )

    @irimi.workflow
    async def read_orders() -> bytes:
        async with (
            aiohttp.ClientSession(auto_decompress=is_decoded) as session,
            session.get(f"{upstream.url}/orders") as answer,
        ):
            return await answer.read()

    content = asyncio.run(read_orders())

    _, call_end = take_call(recorded_events)
    body = call_end["response"]["body"]

    assert call_end["response"]["headers"]["Content-Encoding"] == "gzip"
    assert "flags" not in call_end
    if is_decoded:
        assert content == b'{"ok": true}'
        assert body == {
            "encoding": "text",
            "content_type": "application/json",
            "data": '{"ok": true}',
        }
    else:
        assert content == compressed
        assert (body["encoding"], body["content_encoding"]) == ("base64", "gzip")
        assert base64.b64decode(body["data"]) == compressed


@pytest.mark.parametrize(("method", "status"), [("DELETE", 204), ("HEAD", 200)])
def test_a_response_with_no_body_has_no_body_recorded(
    recorded_events: TakeEvents, upstream: Any, method: str, status: int
) -> None:
    upstream.set_answer("/charges/ch_123", status=status, chunks=[])

    @irimi.workflow
    async def touch_charge() -> bytes:
        async with (
            aiohttp.ClientSession() as session,
            session.request(method, f"{upstream.url}/charges/ch_123") as answer,
        ):
            return await answer.read()

    assert asyncio.run(touch_charge()) == b""

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["response"]["status"] == status
    assert "body" not in call_end["response"]
    assert "flags" not in call_end


def test_many_streams_open_at_once_each_end_on_their_own_call(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    contents = {
        f"/s/{number}": [f"{number}-a".encode(), f"{number}-b".encode()] for number in range(20)
    }
    for path, chunks in contents.items():
        upstream.set_answer(path, chunks=chunks)

    async def read_stream(session: aiohttp.ClientSession, path: str) -> bytes:
        async with session.get(f"{upstream.url}{path}") as answer:
            return b"".join([chunk async for chunk in answer.content.iter_any()])

    @irimi.workflow
    async def read_all_streams() -> list[bytes]:
        async with aiohttp.ClientSession() as session:
            return await asyncio.gather(*(read_stream(session, path) for path in contents))

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


async def send_parts() -> AsyncIterator[bytes]:
    yield b"ab"
    yield b"cd"


def build_form() -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("note", "refund")
    form.add_field("receipt", b"\x00\x01", filename="receipt.bin")
    return form


@pytest.mark.parametrize(
    ("build_options", "recorded_body", "flags", "sent_content"),
    [
        (
            lambda: {"data": b"\xff\xfe\x00"},
            {"encoding": "base64", "content_type": "application/octet-stream", "data": "//4A"},
            None,
            b"\xff\xfe\x00",
        ),
        (
            lambda: {"data": {"amount": "5"}},
            {
                "encoding": "text",
                "content_type": "application/x-www-form-urlencoded",
                "data": "amount=5",
            },
            None,
            b"amount=5",
        ),
        (lambda: {"data": send_parts()}, None, ["request_body_not_recorded"], b"abcd"),
        (lambda: {"data": build_form()}, None, ["request_body_not_recorded"], b"refund"),
        (lambda: {"data": io.BytesIO(b"abcd")}, None, ["request_body_not_recorded"], b"abcd"),
        (
            lambda: {"data": b"abcd" * 100, "compress": True},
            None,
            ["request_body_not_recorded"],
            b"",
        ),
        (lambda: {}, None, None, b""),
    ],
)
def test_a_request_body_held_in_memory_is_recorded_and_any_other_is_flagged(
    recorded_events: TakeEvents,
    upstream: Any,
    build_options: Callable[[], dict[str, Any]],
    recorded_body: dict[str, str] | None,
    flags: list[str] | None,
    sent_content: bytes,
) -> None:
    @irimi.workflow
    async def upload() -> int:
        async with (
            aiohttp.ClientSession() as session,
            session.post(f"{upstream.url}/uploads", **build_options()) as answer,
        ):
            return answer.status

    assert asyncio.run(upload()) == 200

    call_start, _ = take_call(recorded_events)

    assert call_start["request"].get("body") == recorded_body
    assert call_start.get("flags") == flags
    assert upstream.requests[-1][:2] == ("POST", "/uploads")
    assert sent_content in upstream.requests[-1][2]


@pytest.mark.parametrize(
    ("status", "method", "refund", "recorded_body"),
    [
        (302, "GET", None, None),
        (
            307,
            "POST",
            {"amount": 5},
            {"encoding": "text", "content_type": "application/json", "data": '{"amount": 5}'},
        ),
    ],
)
def test_each_hop_of_a_followed_redirect_is_recorded_with_its_body(
    recorded_events: TakeEvents,
    upstream: Any,
    status: int,
    method: str,
    refund: dict[str, int] | None,
    recorded_body: dict[str, str] | None,
) -> None:
    upstream.set_answer("/v1/refunds", status=status, headers=[("Location", "/v2/refunds")])

    @irimi.workflow
    async def refund_ticket() -> int:
        async with (
            aiohttp.ClientSession() as session,
            session.request(method, f"{upstream.url}/v1/refunds", json=refund) as answer,
        ):
            await answer.read()
            return answer.status

    assert asyncio.run(refund_ticket()) == 200

    events = recorded_events()
    first_start, second_start = [event for event in events if event.get("kind") == "http"]
    ends = {event["span_id"]: event for event in events if event["type"] == "end"}
    first_end, second_end = ends[first_start["span_id"]], ends[second_start["span_id"]]

    assert (first_start["name"], second_start["name"]) == (
        f"{method} 127.0.0.1/v1/refunds",
        f"{method} 127.0.0.1/v2/refunds",
    )
    for start in (first_start, second_start):
        assert "flags" not in start
        assert start["request"].get("body") == recorded_body
    assert (first_end["status"], first_end["response"]["status"]) == ("ok", status)
    assert "body" not in first_end["response"]
    assert first_end["flags"] == ["response_body_incomplete"]
    assert second_end["response"]["body"]["data"] == '{"ok": true}'
    assert "flags" not in second_end
    assert upstream.requests[-1] == (
        method,
        "/v2/refunds",
        b'{"amount": 5}' if refund is not None else b"",
    )


def test_a_response_closed_early_is_recorded_as_far_as_it_was_read(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer(
        "/archive", headers=[("Content-Type", "text/plain")], chunks=[b"x" * HALF_A_MEGABYTE] * 3
    )

    @irimi.workflow
    async def peek_at_archive() -> bytes:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{upstream.url}/archive") as answer,
        ):
            return await answer.content.read(10)

    assert asyncio.run(peek_at_archive()) == b"x" * 10

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["flags"] == ["response_body_incomplete"]
    assert call_end["response"]["body"]["data"] == "x" * 10


@pytest.mark.parametrize("is_compressed", [False, True])
def test_an_event_stream_closed_after_an_event_is_recorded_as_whole(
    recorded_events: TakeEvents, upstream: Any, is_compressed: bool
) -> None:
    chunks = [b"data: one\n\ndata: [DONE]\n\n", b"data: never read\n\n"]
    headers = [("Content-Type", "text/event-stream; charset=utf-8")]
    if is_compressed:
        chunks = [gzip.compress(b"".join(chunks))]
        headers.append(("Content-Encoding", "gzip"))
    upstream.set_answer("/completions", headers=headers, chunks=chunks)

    @irimi.workflow
    async def complete() -> list[bytes]:
        lines: list[bytes] = []
        async with (
            aiohttp.ClientSession() as session,
            session.post(f"{upstream.url}/completions") as answer,
        ):
            async for line in answer.content:
                lines.append(line)
                if lines[-2:] == [b"data: [DONE]\n", b"\n"]:
                    break
        return lines

    assert asyncio.run(complete()) == [b"data: one\n", b"\n", b"data: [DONE]\n", b"\n"]

    _, call_end = take_call(recorded_events)

    assert call_end["response"]["body"]["data"] == "data: one\n\ndata: [DONE]\n\n"
    assert "content_encoding" not in call_end["response"]["body"]
    assert "flags" not in call_end


def test_a_response_that_breaks_off_is_recorded_with_its_error(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer("/report", chunks=[b"x" * 1024, b"y"], breaks_off=True)

    @irimi.workflow
    async def read_report() -> bytes:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{upstream.url}/report") as answer,
        ):
            return await answer.read()

    with pytest.raises(aiohttp.ClientPayloadError):
        asyncio.run(read_report())

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "error"
    assert call_end["error"]["type"].endswith("ClientPayloadError")
    assert call_end["response"]["status"] == 200
    assert "body" not in call_end["response"]
    assert call_end["flags"] == ["response_body_incomplete"]


def test_what_was_read_before_a_response_broke_off_is_recorded(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer(
        "/menu", headers=[("Content-Type", "text/plain")], chunks=[b"caf\xc3"], breaks_off=True
    )

    @irimi.workflow
    async def read_menu() -> list[bytes]:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{upstream.url}/menu") as answer,
        ):
            return [chunk async for chunk in answer.content.iter_any()]

    with pytest.raises(aiohttp.ClientPayloadError):
        asyncio.run(read_menu())

    _, call_end = take_call(recorded_events)
    body = call_end["response"]["body"]

    assert (body["encoding"], body["data"]) == ("text", "caf")
    assert call_end["flags"] == ["response_body_incomplete"]


@pytest.mark.parametrize("is_read_repeated", [True, False])
def test_a_read_the_app_gave_up_on_ends_the_call_only_if_it_is_not_repeated(
    recorded_events: TakeEvents, is_read_repeated: bool
) -> None:
    is_second_event_wanted = asyncio.Event()

    async def send_second_event_when_wanted(request: web.Request) -> web.StreamResponse:
        answer = web.StreamResponse(headers={"Content-Type": "text/plain"})
        await answer.prepare(request)
        await answer.write(EVENTS[0])
        await is_second_event_wanted.wait()
        await answer.write(EVENTS[1])
        return answer

    @irimi.workflow
    async def follow_events() -> bytes:
        async with (
            serve("/events", send_second_event_when_wanted) as url,
            aiohttp.ClientSession() as session,
            session.get(f"{url}/events") as answer,
        ):
            content = await answer.content.readuntil(b"\n\n")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(answer.content.readline(), 0.01)
            is_second_event_wanted.set()
            if is_read_repeated:
                content += await answer.content.read()
        return content

    content = asyncio.run(follow_events())

    _, call_end = take_call(recorded_events)

    assert call_end["response"]["body"]["data"] == content.decode()
    if is_read_repeated:
        assert content == ALL_EVENTS
        assert call_end["status"] == "ok"
        assert "flags" not in call_end
    else:
        assert call_end["status"] == "error"
        assert call_end["flags"] == ["response_body_incomplete"]


def test_a_response_that_broke_off_and_is_closed_without_another_read_has_an_end(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer(
        "/report", headers=[("Content-Type", "text/plain")], chunks=[b"x" * 10], breaks_off=True
    )

    @irimi.workflow
    async def peek_at_report() -> bytes:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{upstream.url}/report") as answer,
        ):
            content = await answer.content.read(10)
            while answer.content.exception() is None:
                await asyncio.sleep(0.01)
        return content

    assert asyncio.run(peek_at_report()) == b"x" * 10

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "ok"
    assert call_end["flags"] == ["response_body_incomplete"]
    assert call_end["response"]["body"]["data"] == "x" * 10


def test_a_multipart_response_read_part_by_part_is_recorded_once(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    upstream.set_answer(
        "/export",
        headers=[("Content-Type", "multipart/mixed; boundary=b")],
        chunks=[PARTS[start : start + 50_000] for start in range(0, len(PARTS), 50_000)],
    )

    @irimi.workflow
    async def read_parts() -> list[bytes]:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{upstream.url}/export") as answer,
        ):
            reader = aiohttp.MultipartReader.from_response(answer)
            contents: list[bytes] = []
            while (part := await reader.next()) is not None:
                assert isinstance(part, aiohttp.BodyPartReader)
                contents.append(bytes(await part.read()))
            return contents

    assert asyncio.run(read_parts()) == [b"A" * 300_000, b"B" * 200_000]

    _, call_end = take_call(recorded_events)

    assert call_end["response"]["body"]["data"] == PARTS.decode()
    assert "flags" not in call_end


def test_the_app_reaches_the_readers_own_attributes(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    @irimi.workflow
    async def raise_the_buffer_limit() -> tuple[int, bool]:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{upstream.url}/orders") as answer,
        ):
            answer.content._low_water = 12345
            low_water, _ = answer.content.get_read_buffer_limits()
            await answer.content.readany()
            return low_water, answer.content._eof

    assert asyncio.run(raise_the_buffer_limit()) == (12345, True)


def test_a_request_that_cannot_connect_is_recorded_and_raised_unchanged(
    recorded_events: TakeEvents, unused_url: str
) -> None:
    @irimi.workflow
    async def look_up_charges() -> None:
        async with aiohttp.ClientSession() as session:
            await session.get(f"{unused_url}/charges")

    with pytest.raises(aiohttp.ClientConnectorError):
        asyncio.run(look_up_charges())

    _, call_end = take_call(recorded_events)

    assert call_end["status"] == "error"
    assert call_end["error"]["type"].endswith("ClientConnectorError")
    assert "response" not in call_end


def test_a_call_outside_any_workflow_is_recorded_as_unattached_without_bodies(
    recorded_events: TakeEvents, upstream: Any
) -> None:
    async def report_metrics() -> bytes:
        async with (
            aiohttp.ClientSession() as session,
            session.post(f"{upstream.url}/v1/metrics", json={"cpu": 0.4}) as answer,
        ):
            return await answer.read()

    assert asyncio.run(report_metrics()) == b'{"ok": true}'

    call_start, call_end = recorded_events()

    assert (call_start["kind"], call_start["seq"], call_start["parent_span_id"]) == (
        "http",
        0,
        None,
    )
    assert call_start["flags"] == ["unattached"]
    assert "body" not in call_start["request"]
    assert call_end["response"]["status"] == 200
    assert "body" not in call_end["response"]
    assert "flags" not in call_end
    assert "events_produced" not in call_end


def test_credentials_are_never_recorded(recorded_events: TakeEvents, upstream: Any) -> None:
    credentials = base64.b64encode(b"alice:s3cretpw").decode()
    address = upstream.url.replace("http://", "http://alice:s3cretpw@")
    upstream.set_answer(
        "/charges",
        headers=[
            ("Set-Cookie", "session=abc"),
            ("Set-Cookie", "session=abc"),
            ("X-Note", "caf\xe9"),
        ],
    )

    cookies_the_app_got: list[str] = []

    @irimi.workflow
    async def list_charges() -> None:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{address}/charges", headers={"X-Api-Key": "sk_live_123", "Idempotency-Key": "k-1"}
            ) as answer,
        ):
            cookies_the_app_got.extend(answer.headers.getall("Set-Cookie"))

    asyncio.run(list_charges())

    events = recorded_events()
    _, call_start, call_end, _ = events
    recorded = json.dumps(events)

    assert call_start["request"]["url"] == f"{upstream.url}/charges"
    assert call_start["request"]["headers"]["X-Api-Key"] == REDACTED
    assert call_start["request"]["headers"]["Authorization"] == REDACTED
    assert call_start["request"]["headers"]["Idempotency-Key"] == "k-1"
    assert call_end["response"]["headers"]["Set-Cookie"] == REDACTED
    assert call_end["response"]["headers"]["X-Note"] == "caf\xe9"
    for secret in ("sk_live_123", "s3cretpw", "session=abc", credentials):
        assert secret not in recorded
    assert upstream.request_headers[-1]["Authorization"] == f"Basic {credentials}"
    assert cookies_the_app_got == ["session=abc", "session=abc"]


async def echo_messages(request: web.Request) -> web.WebSocketResponse:
    socket = web.WebSocketResponse()
    await socket.prepare(request)
    async for message in socket:
        await socket.send_str(f"echo {message.data}")
    return socket


def test_a_websocket_handshake_is_recorded_and_ends_when_the_socket_opens(
    recorded_events: TakeEvents,
) -> None:
    @irimi.workflow
    async def greet() -> str:
        async with (
            serve("/ws", echo_messages) as url,
            aiohttp.ClientSession() as session,
            session.ws_connect(f"{url}/ws") as socket,
        ):
            await socket.send_str("hi")
            return await socket.receive_str()

    assert asyncio.run(greet()) == "echo hi"

    call_start, call_end = take_call(recorded_events)

    assert call_start["name"] == "GET 127.0.0.1/ws"
    assert (call_end["status"], call_end["response"]["status"]) == ("ok", 101)
    assert "body" not in call_end["response"]
    assert "flags" not in call_end
