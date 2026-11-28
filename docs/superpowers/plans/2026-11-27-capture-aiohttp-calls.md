# Capture aiohttp Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After `irimi.init()`, every call an app makes through `aiohttp` is recorded as a step of its run, the way a call through `httpx` is.

**Architecture:** `ClientSession._request` is wrapped to append one recording client middleware as the innermost middleware of every request. The middleware turns the `ClientRequest` into the existing `OutboundCall`, hands it to the existing `Interception`, calls the handler, and replaces `response.content` with a recording `StreamReader` subclass that passes every call to the original reader and copies what each read returns. Naming, payloads, flags, credential headers and the size limit are the code that already records `httpx` calls.

**Tech Stack:** Python 3.10+ (SDK), standard library only in the SDK. `aiohttp` (3.14.5 installed, already a development dependency of the workspace) in tests.

**Spec:** `docs/superpowers/specs/2026-11-27-capture-aiohttp-calls-design.md` (the design note for this PR) and `docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md` (piece 4). Read both and `CLAUDE.md` before any task. Read the merged `httpx` patch too: `packages/sdk/src/irimi/_httpx_patch.py`, `_patching.py`, `_interception.py`, `_outbound_calls.py`, `_bodies.py`, and `packages/sdk/tests/conftest.py`, `test_outbound_calls.py`, `test_outbound_bodies.py`, `test_fail_open.py`.

**Scope:** PR 4 of piece 4, issue #20, branch `20-capture-aiohttp-calls` (exists, with the note and this plan committed). One PR, `Closes #20`.

**Already approved, do not reopen:** the fourteen "Needs an OK" items of the piece 4 spec. This plan adds nothing to the wire format, no flag, no field, no SDK dependency, and no public surface beyond `aiohttp` appearing in `InitResult.patched`. If a task cannot be done inside that, stop and report; do not decide it.

## Global Constraints

- `CLAUDE.md` in full. In particular: no comments; docstrings only on the public SDK surface; full names; frozen dataclasses across module boundaries; `mypy --strict`; no `utils` or `helpers`; tests only through `irimi`'s public API and a real `aiohttp` client; no network beyond the loopback address; no API keys.
- The SDK gains no dependency. No SDK module imports `aiohttp` at import time; `_patching.py` imports it with `importlib.import_module` inside `init`, and `patch_aiohttp` receives it as a `types.ModuleType`, so its attributes are `Any`. No `# type: ignore`.
- Nothing the SDK adds around a request may raise into the app or change what the app receives. Each added step is wrapped in `try` / `except Exception` that calls `interception.report_failure(...)` and carries on. An exception `aiohttp` raises is recorded and re-raised unchanged, `asyncio.CancelledError` included.
- One piece of logic records a call. `_aiohttp_patch.py` translates only: it builds `OutboundCall` and `OutboundResponse`, feeds a `ResponseRecording`, and decides nothing about names, flags, payloads or limits.
- Keep changes to `_outbound_calls.py`, `_interception.py`, `_patching.py`, `_httpx_patch.py`, `conftest.py` and the existing tests to the lines this plan names. #18 (LLM recognition) and #19 (`urllib3`) are changing the same files on other branches.
- Before every commit all of these pass: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run mypy --python-version 3.10 packages/sdk/src`, `uv run pytest`.
- Commit messages: imperative, what changed and why, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## Facts about aiohttp the tasks rely on

Read from the installed 3.14.5 source and tried against 3.14.5 and 3.13.0 on 2026-11-27. Check any of them again in `.venv/lib/python3.12/site-packages/aiohttp/` before relying on it for something this plan does not spell out.

- `ClientSession._request(self, method, str_or_url, *, ..., middlewares=None, ...)` is the one method every request goes through, `ws_connect` and `aiohttp.request` included. The middlewares in effect are `middlewares` when it is not `None`, else `self._middlewares`; the last in the sequence is the innermost. A middleware is `async def (request, handler) -> response`, called once per redirect hop and per retry.
- In the middleware, before the handler: `request.method`, `str(request.url)` (query included, no userinfo), `request.headers.items()` (pairs of `str`), `request.body` (a `Payload`, or `b""` when there is none), `request.compress` (truthy when `aiohttp` will compress the body while writing). `await payload.as_bytes()` on a `BytesPayload` returns the bytes it holds.
- After the handler: `response.status`, `response.raw_headers` (pairs of `bytes`), `response.content` (a `StreamReader`; the shared `EmptyStreamReader` when no body is expected). `response.content.at_eof()` is true when no body follows. `response.content.total_compressed_bytes` is `None` unless `aiohttp` is decoding the body; do not read it on a reader that is already at its end (`EmptyStreamReader` does not set it).
- `StreamReader` has `__slots__`. Its public names in 3.14.5: `at_eof`, `begin_http_chunk_receiving`, `end_http_chunk_receiving`, `exception`, `feed_data`, `feed_eof`, `get_read_buffer_limits`, `is_eof`, `iter_any`, `iter_chunked`, `iter_chunks`, `on_eof`, `read`, `read_nowait`, `readany`, `readchunk`, `readexactly`, `readline`, `readuntil`, `set_exception`, `set_read_chunk_size`, `total_bytes`, `total_compressed_bytes`, `total_raw_bytes`, `unread_data`, `wait_eof`. `__aiter__`, `iter_chunked`, `iter_any` and `iter_chunks` only call public read methods on `self`.
- `ClientResponse.close()` and `release()` call `set_exception` on `response.content` when the response was not released before. Nothing else in the client calls `set_exception` on `response.content`; read errors are set on the original reader, which the parser and the connection still hold, and are raised by the next read.
- `await response.read()` on a body that breaks off raises `aiohttp.ClientPayloadError` and returns nothing. A request to a closed port raises `aiohttp.ClientConnectorError`.
- `aiohttp` serialises `json={"amount": 5}` as `{"amount": 5}` (with the space).
- Present from 3.13.0 on: the `middlewares` parameter (from 3.12.0), `BytesPayload.as_bytes`, `StreamReader.total_compressed_bytes`.

## Review Focus

1. **Every way of reading a body.** `read()`, `text()`, `json()`, `async for line in response.content`, `iter_chunked`, `iter_any`, `iter_chunks`, `readexactly`, `readuntil`, `read_nowait`. The app gets exactly what it gets without irimi, the body is recorded once, and one end event is written. (Task 3)
2. **No inherited `StreamReader` method runs on the recording reader** except the four iterator factories. `repr(response.content)` works. An `aiohttp` with an unknown public `StreamReader` name is left unpatched. (Tasks 3, 5)
3. **A decoded body has no `content_encoding`; an undecoded one has it.** (Task 3)
4. **Hops and endings.** Each redirect hop is one call; a released redirect answer ends `ok` and flagged; a 101 ends at once; a response that is closed early ends once. (Task 4)
5. **A session made before `init`, the app's own middlewares, and `middlewares=()` on one request.** All recorded, and the app's middlewares still run. (Tasks 2, 5)

## File Structure

```
packages/sdk/src/irimi/
  _response_recording.py   create: ResponseRecording, end_without_response, moved from _httpx_patch.py
  _httpx_patch.py          modify: import the two names instead of defining them
  _outbound_calls.py       modify: OutboundResponse.is_body_decoded
  _aiohttp_patch.py        create: patch_aiohttp, the middleware, the recording reader
  _patching.py             modify: "aiohttp": patch_aiohttp
packages/sdk/tests/
  test_aiohttp_calls.py    create
  test_outbound_calls.py   modify: the expected InitResult
  test_fail_open.py        modify: an aiohttp app in the two outbound tests; the unknown-version test
```

---

### Task 0: Start from current main

- [ ] `git fetch origin && git rebase origin/main` on `20-capture-aiohttp-calls`. Run `uv sync`.
- [ ] Look at what #18 and #19 changed if either has merged. If `packages/sdk/src/irimi/_response_recording.py` (or a module holding `ResponseRecording` under another name) already exists, skip Task 1 and use what is there. If `Interception.begin` or `name_http_call` changed shape, the middleware in Task 2 calls whatever `_httpx_patch.py` calls now.
- [ ] Run the full check line from Global Constraints. Expected: all pass before any change.

### Task 1: Give the response recording a home both patches can use

A move with no behaviour change. `CLAUDE.md`: a helper moves to a module named for what it does when a second caller appears.

**Files:**
- Create: `packages/sdk/src/irimi/_response_recording.py`
- Modify: `packages/sdk/src/irimi/_httpx_patch.py`

**Interfaces produced:**

```python
# irimi._response_recording
class ResponseRecording:
    def __init__(
        self, interception: Interception, open_call: OpenCall, response: OutboundResponse
    ) -> None: ...
    def add(self, chunk: bytes) -> None: ...
    def end(self, was_read_to_end: bool, error: BaseException | None) -> None: ...


def end_without_response(
    interception: Interception, open_call: OpenCall | None, error: BaseException
) -> None: ...
```

- [ ] **Step 1:** Move `_ResponseRecording` and `_end_without_response` out of `_httpx_patch.py` into the new module, bodies unchanged, renamed `ResponseRecording` and `end_without_response`. `_httpx_patch.py` imports them. Remove imports `_httpx_patch.py` no longer needs (`threading`, `BodyCopy`, `CopiedBody`, `CallOutcome` if unused).
- [ ] **Step 2:** Run the full check line. Expected: every existing test passes unchanged.
- [ ] **Step 3:** `git commit -m "Move the response recording out of the httpx patch so a second patch can use it"`

### Task 2: A call through aiohttp is recorded as a step of its run

**Files:**
- Create: `packages/sdk/src/irimi/_aiohttp_patch.py`
- Modify: `packages/sdk/src/irimi/_patching.py`
- Modify: `packages/sdk/tests/test_outbound_calls.py` (the expected `InitResult` only)
- Test: `packages/sdk/tests/test_aiohttp_calls.py`

**Interfaces produced:**

```python
# irimi._aiohttp_patch
def patch_aiohttp(library: ModuleType, interception: Interception) -> bool: ...


# irimi._patching
LIBRARY_PATCHES: dict[str, Patch | None] = {
    "httpx": patch_httpx,
    "httpx2": patch_httpx,
    "urllib3": None,
    "aiohttp": patch_aiohttp,
}
```

Decisions the signatures leave open:

- `patch_aiohttp` builds everything it needs first and assigns `library.ClientSession._request` last, so a failure part way patches nothing. It returns `False`, patching nothing, unless `"middlewares"` is a parameter of `library.ClientSession._request` (`inspect.signature`), and `library.BytesPayload.as_bytes` and `library.StreamReader.total_compressed_bytes` exist. A missing attribute may simply raise `AttributeError`: `_patching._patch_library` already turns any exception into `unpatched` with the warning.
- The replacement for `_request` is `async def`, wrapped with `functools.wraps`, takes `(session, *arguments, **options)`, and awaits the original with the same arguments and `options["middlewares"]` replaced by `(*effective, record_call)`, where `effective` is `options["middlewares"]` when that is not `None` and `session._middlewares` otherwise. Building the new options is guarded: on any `Exception` it calls `interception.report_failure("record a call made through aiohttp")` and sends with the options as given.
- `record_call(request, send_request)` is the middleware, a closure over `library` and `interception`:
  1. Translate the request and `interception.begin(call)`. Guarded as `_httpx_patch._begin_call` is: on failure `report_failure("read an outbound request")` and carry on with `open_call = None`.
  2. `response = await send_request(request)`. On `BaseException`: `end_without_response(interception, open_call, error)` and re-raise.
  3. Record the response (Task 3 fills this in). Guarded: on failure `report_failure("record an outbound response")`.
  4. Return `response`, the same object.
- Translating the request: `OutboundCall(method=str(request.method), url=str(request.url), headers=tuple((str(name), str(value)) for name, value in request.headers.items()), body=..., has_streamed_body=...)`. In this task `body` is `bytes(await payload.as_bytes())` when `request.body` is an instance of `library.BytesPayload`, else `None`, and `has_streamed_body` is `False`. Task 4 adds streamed and compressed bodies.
- In this task, recording the response is: build `OutboundResponse(status=int(response.status), headers=<raw_headers decoded as latin-1>)`, make a `ResponseRecording`, and end it at once with `was_read_to_end=True`. Bodies come in Task 3. This keeps the task's test honest about what exists: Task 2's test does not assert on the response body.

- [ ] **Step 1: Write the failing tests**

`packages/sdk/tests/test_aiohttp_calls.py`:

```python
import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp
import irimi
from irimi._wire import parse_batch

TakeEvents = Callable[[], list[dict[str, Any]]]


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
    assert run_end["events_produced"] == 4
```

Also in this file, written out in the same style:

```python
def test_calls_made_at_once_each_belong_to_the_workflow(...) -> None: ...
    # an async workflow gathers session.get("/a") and session.get("/b") on one session and
    # reads both; assert two "http" starts named "GET 127.0.0.1/a" and "GET 127.0.0.1/b",
    # both with the workflow as parent, each end on its own span with response.status 200


def test_what_the_apps_own_middlewares_add_is_recorded(...) -> None: ...
    # async def add_tenant(request, handler): request.headers["X-Tenant"] = "acme"; return await handler(request)
    # a session made with ClientSession(middlewares=(add_tenant,)) makes two calls in a workflow:
    #   session.get("/a") and session.get("/b", middlewares=())
    # assert both are recorded; the first start has request.headers["X-Tenant"] == "acme",
    # the second has no "X-Tenant"; upstream.request_headers show the same
```

In `test_outbound_calls.py`, `test_init_reports_the_libraries_it_patched` expects:

```python
patched = "patched=('httpx', 'httpx2', 'aiohttp'), unpatched=('urllib3',)"
```

If #19 has merged, `urllib3` is in `patched` too, in the order `LIBRARY_PATCHES` lists the names, and `unpatched=()`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_aiohttp_calls.py packages/sdk/tests/test_outbound_calls.py -v`
Expected: FAIL. The first test unpacks two events where four are expected; the `InitResult` test still prints `aiohttp` under `unpatched`.

- [ ] **Step 3: Implement the interfaces above**

- [ ] **Step 4: Run the whole suite and the checks**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy && uv run mypy --python-version 3.10 packages/sdk/src`
Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -m "Record a call made through aiohttp as a step of its run"`

### Task 3: The response body, as the app reads it

**Files:**
- Modify: `packages/sdk/src/irimi/_aiohttp_patch.py`, `packages/sdk/src/irimi/_outbound_calls.py`
- Test: `packages/sdk/tests/test_aiohttp_calls.py`

**Interfaces produced:**

```python
# irimi._outbound_calls
@dataclass(frozen=True)
class OutboundResponse:
    status: int
    headers: Headers
    is_body_decoded: bool = False
```

Decisions the signatures leave open:

- In `_outbound_calls.py`, the response body's content encoding is `None` when `response.is_body_decoded`, else the `Content-Encoding` header. Use that one value in both places that read the header for a response today: `_describe_response` (so a decoded body is written without `content_encoding`) and `_was_closed_after_an_event` (so a decoded event stream counts as readable). The request side is untouched. Change nothing else in the file.
- When the handler returns, with `content = response.content`:
  - If `int(response.status) == 101` or `content.at_eof()`: make the `ResponseRecording` and end it with `was_read_to_end=True, error=None`. Do not replace `response.content`.
  - Otherwise: `is_body_decoded = content.total_compressed_bytes is not None`, make the `ResponseRecording`, and set `response.content = recording_reader_class(content, recording)`.
- The recording reader class is built once in `patch_aiohttp` with `type("RecordingStreamReader", (library.StreamReader,), members)`, as `_httpx_patch.py` builds its stream classes (a `class` statement with an `Any` base fails `mypy --strict`). `members` holds:
  - `__slots__ = ("inner_reader", "recording")` and an `__init__(reader, inner_reader, recording)` that sets the two and does not call the base `__init__`.
  - `__repr__`: `repr(reader.inner_reader)`.
  - Passed straight to the inner reader, with `*arguments, **options`: `exception`, `on_eof`, `feed_eof`, `feed_data`, `is_eof`, `at_eof`, `wait_eof`, `unread_data`, `begin_http_chunk_receiving`, `end_http_chunk_receiving`, `get_read_buffer_limits`, `set_read_chunk_size`.
  - Properties that return the inner reader's value: `total_bytes`, `total_compressed_bytes`, `total_raw_bytes`.
  - Reads that copy: `read`, `readany`, `readexactly`, `readline`, `readuntil` (each `async`, awaiting the same method on the inner reader with `*arguments, **options`), `readchunk` (`async`; copies the bytes of the pair it returns), `read_nowait` (not `async`). After a read returns: `recording.add(data)`, then `recording.end(was_read_to_end=True, error=None)` if `inner_reader.at_eof()`; return exactly what the inner reader returned. If the read raises `BaseException`: `recording.end(was_read_to_end=False, error=error)` and re-raise.
  - `set_exception(reader, *arguments, **options)`: `recording.end(was_read_to_end=inner_reader.at_eof(), error=None)`, then, in a `finally`, pass the call to the inner reader.
  - Not in `members`, inherited on purpose: `__aiter__`, `iter_chunked`, `iter_any`, `iter_chunks`.
- `ResponseRecording.add` and `end` already fail open and end only once. The reader adds no `try` of its own around them.
- The check for an unknown `aiohttp`: `patch_aiohttp` returns `False` unless every name in `vars(library.StreamReader)` that does not start with `_` is in `members` or is one of `iter_chunked`, `iter_any`, `iter_chunks`. A subset is fine (an older `aiohttp` lacks some names).

- [ ] **Step 1: Write the failing tests**

```python
EVENTS = [b"data: one\n\n", b"data: two\n\n"]
ALL_EVENTS = b"".join(EVENTS)


async def read_whole(answer: aiohttp.ClientResponse) -> bytes:
    return await answer.read()


async def read_as_text(answer: aiohttp.ClientResponse) -> bytes:
    return (await answer.text()).encode()


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


async def read_once_it_has_all_arrived(answer: aiohttp.ClientResponse) -> bytes:
    await answer.content.wait_eof()
    return answer.content.read_nowait()


@pytest.mark.parametrize(
    "read_answer",
    [
        read_whole,
        read_as_text,
        read_by_line,
        read_in_sevens,
        read_as_it_arrives,
        read_by_http_chunk,
        read_exact_counts,
        read_by_event,
        read_once_it_has_all_arrived,
    ],
)
def test_a_body_is_recorded_whole_however_the_app_reads_it(
    recorded_events: TakeEvents,
    upstream: Any,
    read_answer: Callable[[aiohttp.ClientResponse], Awaitable[bytes]],
) -> None:
    upstream.set_answer(
        "/events", headers=[("Content-Type", "text/event-stream")], chunks=EVENTS
    )

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
```

`take_call` is the helper of the same name in `test_outbound_bodies.py`. Test modules in this repo do not import one another, so define it again in this file and leave `test_outbound_bodies.py` alone. The snippets in Tasks 3 and 4 also need `pytest`, `Awaitable` and `AsyncIterator` from `collections.abc`, and `base64`, `gzip`, `io` and `json` imported as they come into use.

The last read in `read_exact_counts` and `read_by_event` is there on purpose: a call ends as whole when a read finds the reader at its end, and an app that stops at the last byte of a chunked body without one more read may close the response before `aiohttp` has seen the final chunk.

Also in this file:

```python
@pytest.mark.parametrize(
    ("session_options", "recorded_body"),
    [
        ({}, {"encoding": "text", "content_type": "application/json", "data": '{"ok": true}'}),
        ({"auto_decompress": False}, None),
    ],
)
def test_a_compressed_response_is_recorded_as_the_app_read_it(...) -> None: ...
    # the upstream answers /orders with Content-Encoding: gzip and gzip.compress(b'{"ok": true}')
    # the workflow reads answer.read() on aiohttp.ClientSession(**session_options)
    # assert the recorded Content-Encoding header is "gzip" in both cases
    # decoded case: the app got b'{"ok": true}' and body == recorded_body exactly (no content_encoding)
    # undecoded case: body["encoding"] == "base64", body["content_encoding"] == "gzip",
    #   gzip.decompress(base64.b64decode(body["data"])) == b'{"ok": true}'


def test_a_response_larger_than_the_limit_is_cut_and_flagged(...) -> None: ...
    # /export answers text/plain with b"x" * (4 * 1024 * 1024 + 10); the app's answer.read() returns all of it
    # assert len(body["data"]) == 4 * 1024 * 1024 and call_end["flags"] == ["response_body_truncated"]


@pytest.mark.parametrize(("method", "status"), [("DELETE", 204), ("HEAD", 200)])
def test_a_response_with_no_body_has_no_body_recorded(...) -> None: ...
    # as the httpx test of the same name: upstream.set_answer(path, status=status, chunks=[])
    # assert status "ok", response.status, no "body", no "flags"


def test_many_streams_open_at_once_each_end_on_their_own_call(...) -> None: ...
    # as the httpx test of the same name, with twenty session.get calls gathered on one session,
    # each read with iter_any; assert twenty starts, twenty ends, each end's body is its own path's
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_aiohttp_calls.py -v`
Expected: FAIL with `KeyError: 'body'` on the response, because Task 2 ends every call without one.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the whole suite and the checks** (the line from Task 2, Step 4). Expected: all pass, including every `httpx` test unchanged.

- [ ] **Step 5: Commit**

`git commit -m "Record an aiohttp response body as the app reads it, decoded when aiohttp decodes it"`

### Task 4: Request bodies, redirects, and the other ways a call ends

**Files:**
- Modify: `packages/sdk/src/irimi/_aiohttp_patch.py`
- Test: `packages/sdk/tests/test_aiohttp_calls.py`

Decisions:

- The request body: `body = request.body`. Not a `library.BytesPayload` and not empty (`b""`): `body=None, has_streamed_body=True`. A `BytesPayload` with `request.compress` truthy: `body=None, has_streamed_body=True`. A `BytesPayload` otherwise: its bytes, `has_streamed_body=False`; empty bytes are `body=None`. No body: `body=None, has_streamed_body=False`.
- Most of this task is tests. Redirect hops, early closes, errors, the 101 and calls outside a workflow should already behave as the note says once Tasks 2 and 3 are right. A test that fails for another reason than the request body is a bug in Task 2 or 3; fix it there.

- [ ] **Step 1: Write the tests**

```python
async def send_parts() -> AsyncIterator[bytes]:
    yield b"ab"
    yield b"cd"


def build_form() -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("note", "refund")
    form.add_field("receipt", b"\x00\x01", filename="receipt.bin")
    return form


@pytest.mark.parametrize(
    ("build_options", "recorded_body", "flags"),
    [
        (
            lambda: {"data": b"\xff\xfe\x00"},
            {"encoding": "base64", "content_type": "application/octet-stream", "data": "//4A"},
            None,
        ),
        (
            lambda: {"data": {"amount": "5"}},
            {
                "encoding": "text",
                "content_type": "application/x-www-form-urlencoded",
                "data": "amount=5",
            },
            None,
        ),
        (lambda: {"data": send_parts()}, None, ["request_body_not_recorded"]),
        (lambda: {"data": build_form()}, None, ["request_body_not_recorded"]),
        (lambda: {"data": io.BytesIO(b"abcd")}, None, ["request_body_not_recorded"]),
        (
            lambda: {"data": b"abcd" * 100, "compress": True},
            None,
            ["request_body_not_recorded"],
        ),
        (lambda: {}, None, None),
    ],
)
def test_a_request_body_held_in_memory_is_recorded_and_any_other_is_flagged(
    recorded_events: TakeEvents,
    upstream: Any,
    build_options: Callable[[], dict[str, Any]],
    recorded_body: dict[str, str] | None,
    flags: list[str] | None,
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
```

Print the recorded `content_type` of the first two cases once before fixing the expected values; `aiohttp` sets them from the payload. Add an assertion on `upstream.requests[-1][2]` for the generator case (`b"abcd"`), to show the stream reached the server whole.

Also:

```python
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
def test_each_hop_of_a_followed_redirect_is_recorded_with_its_body(...) -> None: ...
    # upstream.set_answer("/v1/refunds", status=status, headers=[("Location", "/v2/refunds")])
    # the workflow: session.request(method, f"{upstream.url}/v1/refunds", json=refund), returns answer.status after answer.read()
    # assert it returns 200; two "http" starts named f"{method} 127.0.0.1/v1/refunds" and ".../v2/refunds";
    # neither start has "flags"; each start's request.get("body") == recorded_body
    # the first hop's end: status "ok", response.status == status, no "body" in response,
    #   flags == ["response_body_incomplete"]   (aiohttp releases a redirect answer unread)
    # the second hop's end: response.body.data == '{"ok": true}', no flags
    # upstream.requests[-1] == (method, "/v2/refunds", b'{"amount": 5}' if refund is not None else b"")


def test_a_response_closed_early_is_recorded_as_far_as_it_was_read(...) -> None: ...
    # /archive answers three chunks of b"x" * 512 * 1024; inside `async with`, the app reads
    # await answer.content.read(10) and leaves
    # assert status "ok", flags == ["response_body_incomplete"], body.data == "x" * 10


@pytest.mark.parametrize("is_compressed", [False, True])
def test_an_event_stream_closed_after_an_event_is_recorded_as_whole(...) -> None: ...
    # /completions answers text/event-stream; charset=utf-8 with
    #   b"data: one\n\ndata: [DONE]\n\n" then b"data: never read\n\n"
    # when is_compressed: one chunk, gzip.compress of all of it, with Content-Encoding: gzip
    # the app reads lines with `async for line in answer.content` and breaks after b"data: [DONE]\n"
    #   has been followed by b"\n"
    # assert body.data == "data: one\n\ndata: [DONE]\n\n" and "flags" not in call_end


def test_a_response_that_breaks_off_is_recorded_with_its_error(...) -> None: ...
    # upstream.set_answer("/report", chunks=[b"x" * 1024, b"y"], breaks_off=True); the app awaits answer.read()
    # with pytest.raises(aiohttp.ClientPayloadError)
    # assert status "error", error.type ends with "ClientPayloadError", response.status == 200,
    #   no "body" in response, flags == ["response_body_incomplete"]


def test_what_was_read_before_a_response_broke_off_is_recorded(...) -> None: ...
    # upstream.set_answer("/menu", chunks=[b"caf\xc3"], breaks_off=True)
    # the app runs: [chunk async for chunk in answer.content.iter_any()] and raises ClientPayloadError
    # assert (body.encoding, body.data) == ("text", "caf") and flags == ["response_body_incomplete"]


def test_a_request_that_cannot_connect_is_recorded_and_raised_unchanged(...) -> None: ...
    # session.get(f"{unused_url}/charges") with pytest.raises(aiohttp.ClientConnectorError)
    # assert status "error", error.type ends with "ClientConnectorError", "response" not in call_end


def test_a_call_outside_any_workflow_is_recorded_as_unattached_without_bodies(...) -> None: ...
    # no workflow: asyncio.run of a coroutine that posts json to /v1/metrics and reads the answer
    # assert start: kind "http", seq 0, parent_span_id None, flags == ["unattached"], no request body;
    # end: response.status 200, no response body, no "flags", no "events_produced"


def test_credentials_are_never_recorded(...) -> None: ...
    # the upstream answers /charges with ("Set-Cookie", "session=abc") twice and ("X-Note", "caf\xe9")
    # the app calls session.get(address + "/charges", headers={"X-Api-Key": "sk_live_123", "Idempotency-Key": "k-1"})
    #   where address is upstream.url with "alice:s3cretpw@" before the host, and returns
    #   answer.headers.getall("Set-Cookie")
    # assert request.url == f"{upstream.url}/charges"; request.headers["X-Api-Key"] and ["Authorization"]
    #   are {"$irimi": "redacted", "reason": "credential"}; ["Idempotency-Key"] == "k-1";
    #   response.headers["Set-Cookie"] is the marker; response.headers["X-Note"] == "caf\xe9";
    #   "sk_live_123", "s3cretpw", "session=abc" and the base64 of b"alice:s3cretpw" are not in json.dumps(events);
    #   the app got ["session=abc", "session=abc"]


def test_a_websocket_handshake_is_recorded_and_ends_when_the_socket_opens(...) -> None: ...
    # an aiohttp.web application with one route, GET /ws, that echoes text messages, started inside
    # the workflow's coroutine with web.AppRunner and web.TCPSite("127.0.0.1", 0); the port is read from
    # runner.addresses. The workflow: session.ws_connect(".../ws"), send_str("hi"), receive_str(), close.
    # assert it returns "echo hi"; exactly one "http" start, named "GET 127.0.0.1/ws";
    # its end: status "ok", response.status == 101, no "body", no "flags"
```

- [ ] **Step 2: Run them**

Run: `uv run pytest packages/sdk/tests/test_aiohttp_calls.py -v`
Expected: the request-body cases for the generator, the form, the file and `compress=` FAIL (no flag yet); the rest PASS if Tasks 2 and 3 are right.

- [ ] **Step 3: Implement the request body rules**

- [ ] **Step 4: Run the whole suite and the checks.** Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -m "Flag aiohttp request bodies that are not held in memory and cover how its calls end"`

### Task 5: Fail open, and an aiohttp the SDK does not know

**Files:**
- Modify: `packages/sdk/tests/test_fail_open.py`
- Modify: `packages/sdk/README.md` or the root `README.md` only if one names the libraries that are recorded (on 2026-11-27 neither did)

- [ ] **Step 1: Write the tests**

In `test_fail_open.py`, next to `OUTBOUND_APP`:

```python
AIOHTTP_APP = """
import asyncio
import os
import aiohttp
import irimi

@irimi.workflow
async def look_up_both(session):
    for path in ("/a", "/b"):
        async with session.get(os.environ["UPSTREAM_URL"] + path) as answer:
            print(await answer.text())

async def main():
    async with aiohttp.ClientSession() as session:
        {setup}
        await look_up_both(session)

asyncio.run(main())
print(irimi.flush())
"""
```

The session is made before `init`. Parametrise the two existing tests over the app, since the cases differ only in it:

```python
@pytest.mark.parametrize("app", [OUTBOUND_APP, AIOHTTP_APP])
def test_a_client_made_before_init_and_a_library_imported_after_it_are_both_recorded(
    fake_store: Any, upstream: Any, app: str
) -> None: ...
    # body unchanged, with OUTBOUND_APP replaced by app
```

and `test_outbound_calls_reach_the_app_unchanged_whatever_the_store_does` gains `app` as a second `parametrize` over the same two. Both keep their names and assertions. An empty `setup` leaves a blank line inside `main`, which is valid Python because the block has another statement.

Add:

```python
UNKNOWN_AIOHTTP_APP = """
import asyncio
import os
import aiohttp
import irimi

aiohttp.StreamReader.read_into = lambda reader, buffer: 0
print(irimi.init(store_url=os.environ["STORE_URL"]).unpatched)

async def main():
    async with aiohttp.ClientSession() as session:
        async with session.get(os.environ["UPSTREAM_URL"] + "/a") as answer:
            print(await answer.text())

asyncio.run(main())
"""


def test_an_aiohttp_irimi_does_not_know_is_left_unpatched_and_works(
    fake_store: Any, upstream: Any
) -> None:
    output = run_script(UNKNOWN_AIOHTTP_APP, STORE_URL=fake_store.url, UPSTREAM_URL=upstream.url)

    assert "'aiohttp'" in output.splitlines()[0]
    assert output.splitlines()[1] == '{"ok": true}'
```

- [ ] **Step 2: Run them**

Run: `uv run pytest packages/sdk/tests/test_fail_open.py -v`
Expected: PASS if Tasks 2 to 4 are right. A failure here is a bug in an earlier task; fix it there.

- [ ] **Step 3: Run the whole suite and the checks, then commit**

`git commit -m "Cover aiohttp calls when the store is away and when aiohttp is a version irimi does not know"`

### Task 6: Run the real thing

No test stands in for this. Do it after Task 5, with the branch's code.

- [ ] **Step 1: The oldest supported aiohttp.** Run `uv run --with aiohttp==3.13.0 python -c "import aiohttp; print(aiohttp.__version__)"` and see `3.13.0`, then `uv run --with aiohttp==3.13.0 pytest packages/sdk/tests/test_aiohttp_calls.py -v`. Expected: all pass. Then `uv run --with aiohttp==3.12.15 python -c "import irimi; print(irimi.init(store_url='http://127.0.0.1:9'))"`. Expected: `aiohttp` under `unpatched`, one warning, no traceback from `init`. If 3.13.0 fails on something version-specific, do not work around it in the SDK: say so in the PR body and name the oldest version that passes. Run `uv sync` afterwards if the environment changed.

- [ ] **Step 2: A real client against a real store.** In a scratch directory outside the repo write two scripts.

  `upstream.py`: a `http.server.ThreadingHTTPServer` on `127.0.0.1:8765` that answers
  - `POST /v1/refunds` with JSON `{"id": "re_456"}`,
  - `GET /orders` with `Content-Encoding: gzip` and a gzip-compressed JSON body,
  - `POST /v1/messages` with `Content-Type: text/event-stream`, chunked, three `data:` events sent a quarter of a second apart,
  - `GET /old` with `302` and `Location: /orders`,
  - `GET /archive` with two megabytes of `x`.

  `app.py`: `irimi.init(store_url="http://127.0.0.1:7717")`, then one `@irimi.workflow` coroutine that, on one `aiohttp.ClientSession`, posts JSON to `/v1/refunds` with an `X-Api-Key` header and reads `json()`; gets `/orders` and reads `json()`; posts to `/v1/messages` and prints each line of `answer.content` as it arrives; gets `/old` and reads `text()`; gets `/archive`, reads `await answer.content.read(100)` and leaves the `async with`. After `asyncio.run`, `print(irimi.flush())`.

  In one terminal, from the repo: `uv run irimi store`. In another: `python upstream.py`. Then, from the repo: `uv run python <scratch>/app.py`, `uv run irimi runs --store-url http://127.0.0.1:7717`, `uv run irimi runs <id> --store-url http://127.0.0.1:7717`, and `uv run irimi runs <id> --json --store-url http://127.0.0.1:7717`. Read two calls whole with `curl http://127.0.0.1:7717/api/v1/runs/<id>/calls/<span_id>`: the gzip one and the event stream.

  Expected:
  - `flush` prints `True`, and the app prints what it would without irimi (run it once with the `init` line removed to compare).
  - The run has six `http` children, in order: `POST 127.0.0.1/v1/refunds`, `GET 127.0.0.1/orders`, `POST 127.0.0.1/v1/messages`, `GET 127.0.0.1/old`, `GET 127.0.0.1/orders`, `GET 127.0.0.1/archive`. (If #18 has merged, `/v1/messages` on `127.0.0.1` is still `http`: recognition matches the provider's host.)
  - `X-Api-Key` is the redaction marker.
  - The gzip call's body is the JSON as text, with no `content_encoding`, and its `Content-Encoding` header is `gzip`.
  - The event stream's body holds all three events, and the lines were printed a quarter of a second apart, not together at the end.
  - The `/old` call has status 302; the `/archive` call is flagged `response_body_incomplete` with 100 bytes of body.

  If any of it differs, stop and fix before opening the PR. Delete `irimi-store.db` from the repo root afterwards if `irimi store` made one there.

- [ ] **Step 3: Open the PR.** `gh pr create` against `main`, body: `Closes #20`, a link to the design note, the departures from the `httpx` behaviour in one list, the supported `aiohttp` versions, and what Steps 1 and 2 showed. Say that no dependency was added. If #18 or #19 merged first and a conflict was resolved, say where.
