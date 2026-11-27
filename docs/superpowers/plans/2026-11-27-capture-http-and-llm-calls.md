# Capture HTTP and LLM Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After `irimi.init()`, every call an app makes through `httpx` or `httpx2` is recorded as a step of its run, and a call to one of four LLM providers is recorded as an `llm` call.

**Architecture:** A patch on each library's transport class turns a request into a library-neutral `OutboundCall` and hands it to one `Interception` object, which records a start and an end through the existing `Recorder`. Pure functions build the payloads: header replacement, body description, naming, LLM recognition. The response's byte stream is wrapped so the body is copied as the app reads it.

**Tech Stack:** Python 3.10+ (SDK), standard library only in the SDK. `httpx`, `httpx2`, `requests` and `aiohttp` as development dependencies of the workspace.

**Spec:** `docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md`. Read it and `CLAUDE.md` before any task.

**Scope:** PRs 1 and 2 of the spec's four. PRs 3 (`urllib3`) and 4 (`aiohttp`) each start with a design note that fixes their patch target, and get their own plan after it.

**Before starting:** the spec lists fourteen items under "Needs an OK". This plan builds all of them as proposed. Do not start Task 1 until Henry has answered them; an answer that differs changes the task named beside it below.

| "Needs an OK" item | Task |
| --- | --- |
| 1, 3, 9, 10 wire additions, no parsed view, `status`, joined headers | 1 |
| 6 credential header list | 1 |
| 12 `patched` and `unpatched` | 1 |
| 5 the numbers, 8 compressed bodies | 2, 3 |
| 4 unattached calls without bodies | 3 |
| 7 Gemini `?key=` (built as: URL recorded as sent) | 1 |
| 2, 11 no model or usage, custom base URLs are `http` | 5 |
| 13 two Step 1 behaviours left out | none |
| 14 store reads in their own issue | 0 |

## Global Constraints

- `CLAUDE.md` in full. In particular: no comments; docstrings only on the public SDK surface; full names; frozen dataclasses across module boundaries; `mypy --strict`; no `utils` or `helpers`; tests only through `irimi`'s public API and a real client of the library; no network beyond the loopback address; no API keys.
- The SDK gains no dependency. `packages/sdk/pyproject.toml` keeps `dependencies = []`. No SDK module imports `httpx`, `httpx2`, `urllib3` or `aiohttp` at import time; a library is imported with `importlib.import_module` inside `init`.
- `import irimi` must still not load `urllib.request` or `importlib.metadata` (`test_importing_irimi_does_not_load_the_modules_only_sending_needs`).
- Nothing the SDK adds around a request may raise into the app or change what the app receives. Each added step is wrapped in `try` / `except Exception` that calls `report_failure` and carries on. An exception the library raises is re-raised unchanged.
- No `# type: ignore`. A patched library is a `types.ModuleType`, so its attributes are `Any`.
- Before every commit all of these pass: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run mypy --python-version 3.10 packages/sdk/src`, `uv run pytest`.
- Exact values from the spec: body limit `4 * 1024 * 1024` bytes; batch size limit `8 * 1024 * 1024`; `answered` is `"live"`; the redaction marker is `{"$irimi": "redacted", "reason": "credential"}`.
- Commit messages: imperative, what changed and why, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.
- Tasks 0 to 4 are PR 1, task 5 is PR 2. Each PR has its own issue and branch `<issue-number>-<slug>`, and its body says `Closes #<n>`.

## Review Focus

1. **A response the app reads in an unusual way.** `response.read()` twice, `.text` after `.content`, `iter_lines()`, `iter_raw()`, an async `aiter_bytes()`. The app gets exactly what it would get without irimi, and one end event is written. (Task 2)
2. **A response with no body.** A 204 and the answer to a `HEAD`. The call ends `ok` with a `response` that has no `body`. (Task 2)
3. **A header value that is not UTF-8.** A server sends `X-Note: caf\xe9`. The call is recorded and the app sees the header as the library gives it. (Task 1)
4. **Many streams open at once.** Twenty concurrent async streamed calls in one workflow. Twenty starts and twenty ends, each end on its own span, every parent the workflow. (Task 2)
5. **A client made before `init`, and a library imported after it.** Both are recorded, because the patch is on the class. (Task 4)

## File Structure

```
pyproject.toml                       modify: dev dependencies httpx, httpx2, requests, aiohttp
packages/sdk/src/irimi/
  _wire/field_names.py               modify: REQUEST RESPONSE ANSWERED, four flag words
  _wire/markers.py                   modify: mark_redacted
  _wire/__init__.py                  modify: export mark_redacted
  _errors.py                         create: describe_error, moved from _labeled_calls.py
  _labeled_calls.py                  modify: describe_error removed
  _decorators.py                     modify: import describe_error from _errors
  _recording.py                      modify: begin_detached_call, end_detached_call
  _credential_headers.py             create: the list, replace_credential_values
  _bodies.py                         create: BODY_LIMIT, CopiedBody, BodyCopy, describe_body
  _outbound_calls.py                 create: OutboundCall, OutboundResponse, CallOutcome, payloads
  _interception.py                   create: Interception (begin, end), OpenCall
  _httpx_patch.py                    create: patch_httpx, the wrapped streams
  _patching.py                       create: PatchReport, patch_installed_libraries
  _llm_providers.py                  create (PR 2): recognise_llm_call
  _sdk.py                            modify: patch at init, InitResult filled in
  _exporter.py                       modify: close a batch by size
packages/sdk/tests/
  conftest.py                        modify: FakeUpstream, upstream and http_library fixtures
  test_outbound_calls.py             create: one call, headers, placement, InitResult
  test_outbound_bodies.py            create: bodies, sizes, streaming, endings
  test_llm_calls.py                  create (PR 2)
  test_fail_open.py                  modify: outbound calls with no store and a dead store
packages/cli/tests/
  test_read_a_run.py                 modify: the end-to-end run gains an outbound call
```

---

## PR 1: capture through httpx and httpx2

### Task 0: Issues and branch

- [ ] Create the issue: `gh issue create --title "Capture outbound HTTP calls through httpx and httpx2" --body "Piece 4, part 1. Spec: docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md"`.
- [ ] If Henry approved item 14, create the store issue: `gh issue create --title "Keep the store's reads cheap when events hold bodies" --body "The tree read, the run list and the replay read load each event whole and page by count. Bound pages by size and read only core columns for the tree and the list. Built before piece 5. See the piece 4 spec, 'The parts and who owns what'."`.
- [ ] `git switch -c <n>-capture-httpx-calls`, then commit the spec and this plan: `git add docs && git commit -m "Add the design and plan for capturing HTTP and LLM calls"`. Leave `egm_context/` and the review reports in the repo root uncommitted.
- [ ] `uv add --dev httpx httpx2 requests aiohttp`, run `uv sync`, and commit `pyproject.toml` and `uv.lock`: "Add the HTTP libraries the SDK patches as development dependencies". The PR body states the reason, as `CLAUDE.md` asks: tests drive a real client of each library.

### Task 1: One outbound call is recorded as a step

**Files:**
- Modify: `packages/sdk/src/irimi/_wire/field_names.py`, `_wire/markers.py`, `_wire/__init__.py`
- Create: `packages/sdk/src/irimi/_errors.py`; modify `_labeled_calls.py`, `_decorators.py`
- Modify: `packages/sdk/src/irimi/_recording.py`, `_sdk.py`
- Create: `packages/sdk/src/irimi/_credential_headers.py`, `_bodies.py`, `_outbound_calls.py`, `_interception.py`, `_httpx_patch.py`, `_patching.py`
- Modify: `packages/sdk/tests/conftest.py`
- Test: `packages/sdk/tests/test_outbound_calls.py`

**Interfaces produced:**

```python
# irimi._wire.field_names
REQUEST = "request"
RESPONSE = "response"
ANSWERED = "answered"
Flag = Literal[
    "not_replayable", "unserialisable_output", "unattached",
    "request_body_truncated", "response_body_truncated",
    "request_body_not_recorded", "response_body_incomplete",
]

# irimi._wire.markers, exported from irimi._wire
def mark_redacted(reason: str) -> dict[str, JsonValue]: ...   # {"$irimi": "redacted", "reason": reason}

# irimi._errors
def describe_error(error: BaseException) -> Payload: ...       # moved unchanged

# irimi._recording.Recorder
def begin_detached_call(self, kind: str, name: str, payload: Payload) -> ActiveCall: ...
def end_detached_call(self, call: ActiveCall, payload: Payload) -> None: ...

# irimi._credential_headers
CREDENTIAL_HEADER_NAMES: frozenset[str]                        # lower case, the 17 names in the spec
def replace_credential_values(headers: Sequence[tuple[str, str]]) -> dict[str, JsonValue]: ...

# irimi._bodies
BODY_LIMIT = 4 * 1024 * 1024

@dataclass(frozen=True)
class CopiedBody:
    content: bytes
    is_truncated: bool

def copy_body(content: bytes) -> CopiedBody: ...
def describe_body(
    body: CopiedBody, content_type: str | None, content_encoding: str | None
) -> dict[str, JsonValue]: ...

# irimi._outbound_calls
@dataclass(frozen=True)
class OutboundCall:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    has_streamed_body: bool

@dataclass(frozen=True)
class OutboundResponse:
    status: int
    headers: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class CallOutcome:
    response: OutboundResponse | None
    body: CopiedBody | None
    is_body_complete: bool
    error: BaseException | None

def name_http_call(call: OutboundCall) -> str: ...
def describe_request(call: OutboundCall, is_attached: bool) -> Payload: ...
def describe_outcome(outcome: CallOutcome, is_attached: bool) -> Payload: ...

# irimi._interception
@dataclass(frozen=True)
class OpenCall:
    call: ActiveCall
    is_attached: bool

class Interception:
    def __init__(self, recorder: Recorder, report_failure: Callable[[str], None]) -> None: ...
    def begin(self, call: OutboundCall) -> OpenCall | None: ...
    def end(self, open_call: OpenCall | None, outcome: CallOutcome) -> None: ...

# irimi._httpx_patch
def patch_httpx(library: ModuleType, interception: Interception) -> bool: ...

# irimi._patching
@dataclass(frozen=True)
class PatchReport:
    patched: tuple[str, ...]
    unpatched: tuple[str, ...]

def patch_installed_libraries(interception: Interception) -> PatchReport: ...

# packages/sdk/tests/conftest.py
@dataclass
class UpstreamAnswer:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=lambda: [("Content-Type", "application/json")])
    chunks: list[bytes] = field(default_factory=lambda: [b'{"ok": true}'])
    breaks_off: bool = False

class FakeUpstream:                  # fixture name: upstream
    url: str                         # http://127.0.0.1:<port>
    answers: dict[str, UpstreamAnswer]   # by path; a path not listed gets UpstreamAnswer()
    requests: list[tuple[str, str, bytes]]   # method, path, body

# fixture http_library: parametrised over "httpx" and "httpx2", yields the imported module
```

Decisions the signatures leave open:

- `replace_credential_values` keeps names as sent. Two headers whose names differ only in case are one header, under the first spelling, values joined with `, `. A credential's value is `mark_redacted("credential")` however many times it was sent. The result goes through `escape_reserved_key`.
- `name_http_call` is `"{method} {host}{path}"` with the host lower-cased and without port or userinfo, from `urllib.parse.urlsplit`. An empty path is `/`.
- `describe_request` writes `request` with `method`, `url`, `headers`, and `body` when `call.body` is not `None` and not empty. When `is_attached` is false it adds the `unattached` flag. `Payload.approximate_size` is the length of the recorded body bytes plus the lengths of the URL and header values.
- `describe_outcome` writes `status`, `answered: "live"`, and `response` (`status`, `headers`, `body`) when a response arrived. With `error` set it starts from `describe_error(error)`.
- `Interception.begin` reads `recorder.find_current_call()` to set `is_attached`, calls `begin_detached_call("http", name, payload)`, and returns `None` on any failure after reporting it. `end` does nothing for `None`.
- `begin_detached_call` does what `begin_call` does without setting the current call. `end_detached_call` does what `end_call` does without resetting it and never writes `events_produced`. `begin_call` and `end_call` are rewritten on top of them, so the event-building code exists once.
- `patch_httpx` returns `False`, patching nothing, unless the library has `HTTPTransport.handle_request`, `AsyncHTTPTransport.handle_async_request`, `SyncByteStream`, `AsyncByteStream` and `RequestNotRead`. The request's headers are read from `request.headers.raw` and decoded as latin-1; the body from `request.content`, with `RequestNotRead` meaning a streamed body.
- The two wrapped stream classes are built with `type(name, (library.SyncByteStream,), namespace)` and the async equivalent, once per patched library. A `class` statement with an `Any` base fails `mypy --strict`, and `httpx` raises unless the response's stream is an instance of its own stream class.
- In this task the wrapped stream copies every chunk and ends the call when iteration finishes or the stream is closed, whichever is first. Task 2 adds the limit and the other endings.
- `patch_installed_libraries` goes through `("httpx", "httpx2", "urllib3", "aiohttp")` in that order. A name `importlib.util.find_spec` does not find is skipped. `httpx` and `httpx2` are imported and passed to `patch_httpx`; `False`, or an import that raises, puts the name in `unpatched` and logs one warning on the `irimi` logger. `urllib3` and `aiohttp` go to `unpatched` without being imported.
- `Sdk._start_recording` builds the `Interception` with the recorder and `self.report_failure`, calls `patch_installed_libraries`, and `Sdk.init` returns `InitResult("normal", report.patched, report.unpatched)`. A second `init` returns the first result. Without a store URL both tuples are empty.

- [ ] **Step 1: Add `FakeUpstream`, `upstream` and `http_library` to `conftest.py`**

A `ThreadingHTTPServer` on `127.0.0.1`, port 0, in the style of `FakeStore`. It handles every method, reads the request body by `Content-Length` or chunked encoding, records `(method, path, body)` with the path taken from `urlsplit(self.path).path` so a proxied absolute URL works, and writes the answer for that path. One chunk is sent with `Content-Length`. Several chunks are sent with `Transfer-Encoding: chunked`, flushing after each. `breaks_off` closes the socket after the first chunk without the final zero chunk.

- [ ] **Step 2: Write the failing tests**

```python
@irimi.function
def note_refund(refund: dict[str, Any]) -> str:
    return refund["id"]


def test_an_outbound_call_is_recorded_as_a_step_of_its_run(
    recorded_events: TakeEvents, upstream: FakeUpstream, http_library: Any
) -> None:
    upstream.answers["/v1/refunds"] = UpstreamAnswer(chunks=[b'{"id": "re_456"}'])

    @irimi.workflow
    def refund_ticket(amount: int) -> str:
        with http_library.Client() as client:
            answer = client.post(f"{upstream.url}/v1/refunds?expand=charge", json={"amount": amount})
        return note_refund(answer.json())

    assert refund_ticket(5) == "re_456"

    run_start, call_start, call_end, note_start, _, run_end = recorded_events()

    assert parse_batch({"events": [call_start, call_end]}).refused == ()
    assert (call_start["kind"], call_start["name"], call_start["seq"]) == (
        "http", "POST 127.0.0.1/v1/refunds", 1,
    )
    assert call_start["parent_span_id"] == run_start["span_id"]
    assert call_start["request"]["method"] == "POST"
    assert call_start["request"]["url"] == f"{upstream.url}/v1/refunds?expand=charge"
    assert call_start["request"]["body"] == {
        "encoding": "text", "content_type": "application/json", "data": '{"amount":5}',
    }
    assert (call_end["span_id"], call_end["status"], call_end["answered"]) == (
        call_start["span_id"], "ok", "live",
    )
    assert call_end["response"]["status"] == 200
    assert call_end["response"]["headers"]["Content-Type"] == "application/json"
    assert call_end["response"]["body"]["data"] == '{"id": "re_456"}'
    assert note_start["parent_span_id"] == run_start["span_id"]
    assert run_end["events_produced"] == 6


def test_an_async_client_is_recorded_the_same_way(
    recorded_events: TakeEvents, upstream: FakeUpstream, http_library: Any
) -> None: ...
    # an async workflow gathers two client.get calls to /a and /b on one AsyncClient
    # assert: two "http" starts named "GET 127.0.0.1/a" and "GET 127.0.0.1/b",
    # both with the workflow as parent, each end on its own span with response.status 200


@pytest.mark.parametrize(
    "name",
    ["Authorization", "authorization", "Proxy-Authorization", "Cookie", "X-Api-Key", "x-api-key",
     "Api-Key", "Apikey", "X-Goog-Api-Key", "X-Auth-Token", "X-Auth-Key", "X-Amz-Security-Token",
     "Ocp-Apim-Subscription-Key", "X-Functions-Key", "Private-Token", "X-Vault-Token",
     "X-Shopify-Access-Token", "DD-API-KEY"],
)
def test_the_value_of_a_credential_header_is_never_recorded(
    recorded_events: TakeEvents, upstream: FakeUpstream, http_library: Any, name: str
) -> None: ...
    # get /charges with headers {name: "sk_live_123", "Idempotency-Key": "k-1"}
    # the upstream answers with ("Set-Cookie", "session=abc") sent twice and ("X-Note", "caf\xe9")
    # assert request.headers[name] == {"$irimi": "redacted", "reason": "credential"}
    # assert request.headers["Idempotency-Key"] == "k-1"
    # assert response.headers["Set-Cookie"] == {"$irimi": "redacted", "reason": "credential"}
    # assert response.headers["X-Note"] == "caf\xe9" and call_end["status"] == "ok"
    # assert "sk_live_123" not in json.dumps(events) and "session=abc" not in json.dumps(events)
    # assert upstream.requests[-1] reached the server and the app saw answer.headers["set-cookie"]


def test_a_header_sent_twice_is_recorded_once_with_its_values_joined(...) -> None: ...
    # the upstream answers with ("Vary", "Accept") and ("Vary", "Origin")
    # assert response.headers["Vary"] == "Accept, Origin"


def test_init_reports_the_libraries_it_patched(fake_store: Any) -> None: ...
    # a subprocess, as test_fail_open.run_app does:
    #   print(irimi.init(store_url=...)) prints
    #   InitResult(mode='normal', patched=('httpx', 'httpx2'), unpatched=('urllib3', 'aiohttp'))
    # a second irimi.init() in the same process prints the same
    # with no store URL it prints InitResult(mode='normal', patched=(), unpatched=())
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_outbound_calls.py -v`
Expected: FAIL. The first test unpacks two events where six are expected.

- [ ] **Step 4: Implement the interfaces above**

- [ ] **Step 5: Run the whole suite and the checks**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy && uv run mypy --python-version 3.10 packages/sdk/src`
Expected: all pass, including every test in `test_recording.py` unchanged.

- [ ] **Step 6: Commit**

`git commit -m "Record a call made through httpx or httpx2 as a step of its run"`

### Task 2: Bodies, sizes and how a call ends

**Files:**
- Modify: `packages/sdk/src/irimi/_bodies.py`, `_outbound_calls.py`, `_httpx_patch.py`
- Test: `packages/sdk/tests/test_outbound_bodies.py`

**Interfaces:**
- Consumes: everything Task 1 produced.
- Produces:

```python
# irimi._bodies
class BodyCopy:
    def add(self, chunk: bytes) -> None: ...      # keeps at most BODY_LIMIT bytes
    def take(self) -> CopiedBody: ...
```

Decisions the signatures leave open:

- `describe_body` writes `encoding: "text"` and the decoded string when the bytes are valid UTF-8, else `encoding: "base64"`. When the body is truncated and only its last one to three bytes fail to decode, those bytes are dropped and it is still `text`. `content_encoding` is written only when the `Content-Encoding` header is present and not `identity`. The SDK never decompresses.
- `describe_request` adds `request_body_truncated` when the copy was cut, and `request_body_not_recorded` with no `body` when `has_streamed_body`.
- `describe_outcome` adds `response_body_truncated` when the copy was cut and `response_body_incomplete` when `is_body_complete` is false. With both an error and a response it writes `status: "error"`, the `error`, and the partial `response`.
- The wrapped stream ends the call exactly once, on the first of: iteration finishes (`is_body_complete` true); iteration raises (the error, incomplete, re-raised unchanged); `close` or `aclose` before iteration finished (incomplete, `ok`); `close` after it finished (nothing more). When the transport call itself raises, the patch ends the call with the error and no response.

- [ ] **Step 1: Write the failing tests**

Each test runs inside a small `@irimi.workflow` and reads the one `http` start and end from `recorded_events()`. `LIMIT = 4 * 1024 * 1024`.

```python
def test_a_compressed_response_is_recorded_as_it_arrived(...) -> None: ...
    # answer: headers Content-Type application/json, Content-Encoding gzip; chunks [gzip.compress(b'{"ok": true}')]
    # assert the app's answer.json() == {"ok": True}
    # assert body["encoding"] == "base64" and body["content_encoding"] == "gzip"
    # assert gzip.decompress(base64.b64decode(body["data"])) == b'{"ok": true}'
    # assert response.headers["Content-Encoding"] == "gzip"


def test_a_binary_body_is_recorded_as_base64(...) -> None: ...
    # post content=b"\xff\xfe\x00" with Content-Type application/octet-stream
    # assert request.body == {"encoding": "base64", "content_type": "application/octet-stream", "data": "//4A"}


@pytest.mark.parametrize(
    ("content", "recorded_length"),
    [(b"x" * (LIMIT + 10), LIMIT), (b"a" + "é".encode() * (LIMIT // 2), LIMIT - 1)],
)
def test_a_response_larger_than_the_limit_is_cut_and_flagged(...) -> None: ...
    # assert the app's answer.content == content
    # assert body["encoding"] == "text" and len(body["data"].encode()) == recorded_length
    # assert call_end["flags"] == ["response_body_truncated"]


def test_a_request_larger_than_the_limit_is_cut_and_flagged(...) -> None: ...
    # post content=b"x" * (LIMIT + 10); assert upstream.requests[-1][2] has LIMIT + 10 bytes
    # assert len(request.body["data"]) == LIMIT and call_start["flags"] == ["request_body_truncated"]


def test_a_streamed_request_body_is_not_recorded(...) -> None: ...
    # post content=iter([b"ab", b"cd"]); assert upstream.requests[-1][2] == b"abcd"
    # assert "body" not in request and call_start["flags"] == ["request_body_not_recorded"]


def test_a_streamed_response_is_recorded_whole_when_the_stream_ends(...) -> None: ...
    # answer chunks [b"data: one\n\n", b"data: two\n\n"], Content-Type text/event-stream
    # inside client.stream("POST", ...): assert list(answer.iter_lines()) is what httpx gives unpatched
    # assert body["data"] == "data: one\n\ndata: two\n\n" and call_end["status"] == "ok"
    # assert "flags" not in call_end


def test_a_response_closed_early_is_recorded_as_far_as_it_was_read(...) -> None: ...
    # answer chunks: three of b"x" * 512 * 1024
    # inside client.stream(...): take one chunk from answer.iter_raw(), then leave the block
    # assert call_end["status"] == "ok" and call_end["flags"] == ["response_body_incomplete"]
    # assert 0 < len(body["data"]) < 3 * 512 * 1024


def test_a_response_that_breaks_off_is_recorded_with_its_error(...) -> None: ...
    # answer chunks [b"x" * 1024, b"y"], breaks_off=True
    # with pytest.raises(http_library.RemoteProtocolError): client.get(...)
    # assert call_end["status"] == "error" and call_end["error"]["type"].endswith("RemoteProtocolError")
    # assert call_end["response"]["status"] == 200 and call_end["flags"] == ["response_body_incomplete"]


def test_a_request_that_cannot_be_sent_is_recorded_and_raised_unchanged(...) -> None: ...
    # get a loopback port nothing listens on; with pytest.raises(http_library.ConnectError)
    # assert call_end["status"] == "error" and "response" not in call_end


@pytest.mark.parametrize(("method", "status"), [("DELETE", 204), ("HEAD", 200)])
def test_a_response_with_no_body_has_no_body_recorded(...) -> None: ...
    # answer status=status, chunks=[]; assert call_end["status"] == "ok"
    # assert call_end["response"]["status"] == status and "body" not in call_end["response"]


def test_a_response_read_more_than_once_is_recorded_once(...) -> None: ...
    # answer = client.get(...); answer.read(); answer.read(); answer.text; answer.content
    # assert exactly one end event for the call's span, body complete


def test_many_streams_open_at_once_each_end_on_their_own_call(...) -> None: ...
    # an async workflow gathers 20 tasks, each: async with client.stream("GET", f"/s/{n}") as answer:
    #   b"".join([chunk async for chunk in answer.aiter_bytes()])
    # each path answers with its own two chunks
    # assert 20 starts and 20 ends, each end's span_id is a distinct start's span_id,
    # every parent_span_id is the workflow's, and each end's body data is that path's content
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_outbound_bodies.py -v`
Expected: FAIL on the flags, the cut and the error cases.

- [ ] **Step 3: Implement `BodyCopy`, the text cut, the flags and the stream's endings**

- [ ] **Step 4: Run the whole suite and the checks**

Run: the five commands in Global Constraints.
Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -m "Record bodies up to 4 MB and end a call when its response ends"`

### Task 3: Calls outside a workflow, and batches closed by size

**Files:**
- Modify: `packages/sdk/src/irimi/_outbound_calls.py`, `_exporter.py`
- Test: `packages/sdk/tests/test_outbound_calls.py`, `packages/sdk/tests/test_fail_open.py`

**Interfaces:**
- Consumes: `describe_request(call, is_attached)`, `describe_outcome(outcome, is_attached)` from Task 1.
- Produces:

```python
# irimi._exporter
BATCH_SIZE_LIMIT = 8 * 1024 * 1024
```

Decisions the signatures leave open:

- When `is_attached` is false, `describe_request` and `describe_outcome` leave `body` out. The `unattached` flag is already written by Task 1. Neither writes a truncation or not-recorded flag for a body it left out. `response_body_incomplete` is still written.
- `Exporter._take_batch` takes events from the front while the batch has fewer than `BATCH_SIZE` events and less than `BATCH_SIZE_LIMIT` by `approximate_size`. It always takes at least one. `_is_send_due` is also true when `_pending_size >= BATCH_SIZE_LIMIT`, and `enqueue` wakes the sender when a new event takes the pending size past that limit.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_call_outside_any_workflow_is_recorded_as_unattached_without_bodies(
    recorded_events: TakeEvents, upstream: FakeUpstream, http_library: Any
) -> None:
    with http_library.Client() as client:
        client.post(f"{upstream.url}/v1/metrics", json={"cpu": 0.4})

    call_start, call_end = recorded_events()

    assert (call_start["kind"], call_start["seq"], call_start["parent_span_id"]) == ("http", 0, None)
    assert call_start["flags"] == ["unattached"]
    assert call_start["request"]["url"] == f"{upstream.url}/v1/metrics"
    assert "body" not in call_start["request"]
    assert call_end["response"]["status"] == 200
    assert "body" not in call_end["response"]
    assert "events_produced" not in call_end


# test_fail_open.py, a subprocess app in the style of TWO_RUNS_TOO_LARGE_TO_WAIT_TOGETHER
def test_large_calls_are_sent_in_batches_no_larger_than_the_limit(fake_store: Any, ...) -> None: ...
    # the app: one workflow makes six GETs whose answers are 3 MB each, then prints irimi.flush()
    # the upstream is a FakeUpstream started by the test, its URL passed in the environment
    # assert output ends "True\n"
    # assert len(fake_store.batches) >= 2
    # assert every batch holds at most three events that carry a response body
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_outbound_calls.py packages/sdk/tests/test_fail_open.py -v`
Expected: FAIL. The unattached call has a `body`, and a batch holds more than three of the six answers.

- [ ] **Step 3: Implement both**

- [ ] **Step 4: Run the whole suite and the checks**

Run: the five commands in Global Constraints.
Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -m "Record unattached calls without bodies and close a batch at 8 MB"`

### Task 4: Fail open, end to end, and the real thing

**Files:**
- Modify: `packages/sdk/tests/test_fail_open.py`, `packages/cli/tests/test_read_a_run.py`
- Modify: `packages/sdk/README.md` if it describes what is recorded

**Interfaces:**
- Consumes: everything above. Produces nothing new.

- [ ] **Step 1: Write the tests**

```python
# test_fail_open.py. OUTBOUND_APP is a subprocess app:
#   client = httpx2.Client()            (made before irimi.init)
#   irimi.init(...)
#   import httpx                        (imported after irimi.init)
#   a workflow calls client.get(UPSTREAM + "/a").text and httpx.get(UPSTREAM + "/b").text, prints both
#   print(irimi.flush())

def test_a_client_made_before_init_and_a_library_imported_after_it_are_both_recorded(...) -> None: ...
    # assert output == '{"ok": true}\n{"ok": true}\nTrue\n'
    # assert the names of the two http starts are "GET 127.0.0.1/a" and "GET 127.0.0.1/b"


@pytest.mark.parametrize("setup", ["", "irimi.init()", "irimi.init(store_url=<a dead port>)"])
def test_outbound_calls_reach_the_app_unchanged_whatever_the_store_does(...) -> None: ...
    # assert the two answers are printed as above; flush prints True, True, False in turn
```

In `packages/cli/tests/test_read_a_run.py`, the `APP` workflow gains one outbound call to the store it records to, so no second server is needed:

```python
import httpx2
...
@irimi.workflow
def handle_ticket(ticket):
    httpx2.get(os.environ["IRIMI_STORE_URL"] + "/api/v1/runs")
    record_refund(ticket["id"])
    return "Refund issued"
```

and the test asserts:

```python
    children = root["children"]
    assert [(child["kind"], child["name"]) for child in children] == [
        ("http", "GET 127.0.0.1/api/v1/runs"), ("function", "record_refund"),
    ]
    call_url = f"{local_store_url}/api/v1/runs/{run_id}/calls/{children[0]['span_id']}"
    with urllib.request.urlopen(call_url) as answer:
        call = json.loads(answer.read())
    # assert the call document holds a response status of 200 and a request url ending "/api/v1/runs"
```

The CLI has no command for one call, so the test reads the store's call read (`CALL_PATH` in `irimi_store.reader_documents`) directly, as it already reads the list. Look at the document `read_call` returns before writing the last assertion.

- [ ] **Step 2: Run them**

Run: `uv run pytest packages/sdk/tests/test_fail_open.py packages/cli/tests/test_read_a_run.py -v`
Expected: PASS if Tasks 1 to 3 are right. A failure here is a bug in an earlier task; fix it there.

- [ ] **Step 3: Run the real thing**

No test stands in for this. In one terminal: `uv run irimi store`. In a scratch directory outside the repo, write a script that calls `irimi.init(store_url="http://127.0.0.1:7717")`, then inside a workflow calls `anthropic.Anthropic(api_key="not-a-key", base_url=<a local server that answers /v1/messages with a canned JSON message>).messages.create(...)`, once plain and once with `stream=True`. Install `anthropic` into the scratch environment only, not the workspace. Then run `uv run irimi runs` and `uv run irimi runs <id> --json`.

Expected: the run lists two `http` children named `POST 127.0.0.1/v1/messages`; `X-Api-Key` is the redaction marker; the streamed call's response body holds every event the script received. Report what was seen in the PR body. If any of it differs, stop and fix before opening the PR.

- [ ] **Step 4: Run the whole suite and the checks, then commit and open the PR**

`git commit -m "Cover outbound calls end to end and when the store is away"`, then `gh pr create` against `main` with `Closes #<n>`, the reason for the four development dependencies, and what Step 3 showed.

---

## PR 2: LLM recognition

### Task 5: A call to a provider is recorded as an `llm` call

**Files:**
- Create: `packages/sdk/src/irimi/_llm_providers.py`
- Modify: `packages/sdk/src/irimi/_outbound_calls.py`, `_interception.py`
- Test: `packages/sdk/tests/test_llm_calls.py`

**Interfaces:**
- Consumes: `OutboundCall`, `name_http_call`, `Interception.begin` from Task 1.
- Produces:

```python
# irimi._llm_providers
def recognise_llm_call(method: str, host: str, path: str) -> str | None: ...   # the name, or None

# irimi._outbound_calls
@dataclass(frozen=True)
class CallIdentity:
    kind: Literal["http", "llm"]
    name: str

def identify_call(call: OutboundCall) -> CallIdentity: ...
```

Decisions the signatures leave open:

- The table is the six rows in the spec's "Recognising an LLM call", as data: host, a tuple of path segments in which `{version}` and `{model}` match any one segment, and the name. The Gemini segment is matched by its ending, `:generateContent` or `:streamGenerateContent`.
- A row matches when the host is equal ignoring case and the path has the same segments. `method` is taken and not compared, so the function's inputs are what the spec says piece 5 will pass.
- `identify_call` returns `CallIdentity("llm", name)` when recognised, else `CallIdentity("http", name_http_call(call))`. `Interception.begin` uses it for `begin_detached_call`.

- [ ] **Step 0: Issue and branch**

`gh issue create --title "Recognise calls to LLM providers" --body "Piece 4, part 2. Spec: docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md"`, then `git switch -c <n>-recognise-llm-calls` from `main` after PR 1 has merged.

- [ ] **Step 1: Write the failing tests**

The provider is never contacted: the client sends plain HTTP to the provider's host through `upstream` as its proxy, so the patched transport sees the provider's host and the local server answers.

```python
@pytest.mark.parametrize(
    ("url", "kind", "name"),
    [
        ("http://api.anthropic.com/v1/messages", "llm", "anthropic messages.create"),
        ("http://API.Anthropic.com/v1/messages?beta=true", "llm", "anthropic messages.create"),
        ("http://api.openai.com/v1/chat/completions", "llm", "openai chat.completions.create"),
        ("http://api.openai.com/v1/responses", "llm", "openai responses.create"),
        ("http://openrouter.ai/api/v1/chat/completions", "llm", "openrouter chat.completions.create"),
        ("http://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro:generateContent",
         "llm", "gemini models.generate_content"),
        ("http://generativelanguage.googleapis.com/v1/models/gemini-3-pro:streamGenerateContent?alt=sse",
         "llm", "gemini models.generate_content"),
        ("http://api.anthropic.com/v1/messages/count_tokens", "http",
         "POST api.anthropic.com/v1/messages/count_tokens"),
        ("http://api.openai.com/v1/embeddings", "http", "POST api.openai.com/v1/embeddings"),
        ("http://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro:countTokens", "http",
         "POST generativelanguage.googleapis.com/v1beta/models/gemini-3-pro:countTokens"),
        ("http://my-gateway.internal/v1/chat/completions", "http",
         "POST my-gateway.internal/v1/chat/completions"),
    ],
)
def test_a_call_is_named_by_its_provider_and_operation(
    recorded_events: TakeEvents, upstream: FakeUpstream, http_library: Any,
    url: str, kind: str, name: str,
) -> None:
    @irimi.workflow
    def ask() -> int:
        with http_library.Client(proxy=upstream.url) as client:
            return client.post(url, json={"model": "claude-opus-5-5", "messages": []}).status_code

    assert ask() == 200

    _, call_start, call_end, _ = recorded_events()

    assert (call_start["kind"], call_start["name"]) == (kind, name)
    assert call_start["request"]["url"] == url
    assert "claude-opus-5-5" in call_start["request"]["body"]["data"]
    assert "gen_ai.request.model" not in call_start["request"]
    assert "usage" not in call_end
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_llm_calls.py -v`
Expected: the seven `llm` cases FAIL with `kind == "http"`; the four `http` cases pass.

- [ ] **Step 3: Implement `recognise_llm_call` and `identify_call`**

- [ ] **Step 4: Run the real thing again**

Repeat Task 4 Step 3 with one change: the client's `base_url` is `http://api.anthropic.com` and the environment sets `HTTP_PROXY` to the local server, so the call is addressed to the provider and answered locally.

Expected: `irimi runs <id>` shows the two children as `llm anthropic messages.create`.

- [ ] **Step 5: Run the whole suite and the checks, commit and open the PR**

`git commit -m "Record a call to a known LLM provider as an llm call"`, then `gh pr create` against `main` with `Closes #<n>`.
