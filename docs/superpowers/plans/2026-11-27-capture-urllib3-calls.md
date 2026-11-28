# Capture urllib3 Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After `irimi.init()`, every call an app makes through `urllib3`, and so through `requests`, is recorded as a step of its run the way an `httpx` call is.

**Architecture:** A patch on `HTTPConnectionPool._make_request` turns one request on the wire into an `OutboundCall` and hands it to the existing `Interception`. Four methods of `HTTPResponse` are replaced to copy the body as it arrives and to end the call once. Nothing about how a call is recorded is new: the patch only translates.

**Tech Stack:** Python 3.10+ (SDK), standard library only in the SDK. `requests` and `urllib3` as development dependencies of the workspace.

**Spec:** `docs/superpowers/specs/2026-11-27-capture-urllib3-calls-design.md` (the design note for this PR) and its parent, `docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md`. Read both and `CLAUDE.md` before any task. Read the merged `httpx` patch too: `packages/sdk/src/irimi/_httpx_patch.py`, `_patching.py`, `_interception.py`, `_outbound_calls.py`, `_bodies.py`.

**Scope:** PR 3 of the parent's four, issue #19, branch `19-capture-urllib3-calls` (already created, with the design note and this plan committed). One PR.

**Nothing waits on an OK.** The design note stays inside what Henry approved on 2026-11-27. If a task seems to need a wire-format addition, a change to the public SDK surface, or an SDK dependency, stop and report; do not decide it.

**Built at the same time:** #18 (LLM recognition, in `_outbound_calls.py` and `_interception.py`) and #20 (`aiohttp`). Keep edits to files they share as small as this plan says, so the branches merge cleanly. Do not touch `_outbound_calls.py`, `_interception.py` or `_bodies.py`.

## Global Constraints

- `CLAUDE.md` in full. In particular: no comments; docstrings only on the public SDK surface; full names; frozen dataclasses across module boundaries; `mypy --strict`; no `utils` or `helpers`; tests only through `irimi`'s public API and a real client of the library; no network beyond the loopback address; no API keys.
- The SDK gains no dependency. `packages/sdk/pyproject.toml` keeps `dependencies = []`. No SDK module imports `urllib3` or `requests`; the library reaches the patch as the `ModuleType` that `_patching.py` imported.
- Nothing the SDK adds around a request may raise into the app or change what the app receives. Each added step is wrapped in `try` / `except Exception` that calls `interception.report_failure` and carries on. The original method's return value, and any exception it raises, pass through unchanged.
- No `# type: ignore`. A patched library is a `types.ModuleType`, so its attributes are `Any`.
- No module-level mutable state. The thread-local slot is created inside `patch_urllib3`.
- The standard library's `http.client` is never patched. The SDK's own sending goes through it.
- Before every commit all of these pass: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run mypy --python-version 3.10 packages/sdk/src`, `uv run pytest`.
- Commit messages: imperative, what changed and why, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## Review Focus

1. **A chunked response that breaks off.** `HTTPResponse._update_chunk_length` calls `self.close()` and then raises. The call must end with that error, not as a response the app closed. (Task 2)
2. **A response `urllib3` reads before returning it.** `urllib3.request("GET", url)` reads the whole body inside `_make_request`. The body is recorded, compressed as it arrived, and one end event is written. (Task 1)
3. **Several threads at once.** Eight threads each make a preloaded request. Each end is on its own call. (Task 2)
4. **What the app sees.** Every `requests` and `urllib3` call in the tests returns what it returns without irimi: the JSON, the bytes, the lines, the exception type.
5. **A session made before `init`.** It is recorded, because the patch is on the class. (Task 3)

## File Structure

```
pyproject.toml                       modify: urllib3 named in the http-clients group
uv.lock                              modify: by uv
packages/sdk/src/irimi/
  _response_recording.py             create: ResponseRecording, moved from _httpx_patch.py
  _httpx_patch.py                    modify: import ResponseRecording instead of defining it
  _urllib3_patch.py                  create: patch_urllib3
  _patching.py                       modify: urllib3 maps to patch_urllib3
packages/sdk/tests/
  conftest.py                        modify: the sync_http_library fixture
  test_outbound_calls.py             modify: five tests run for requests too; the init report
  test_outbound_bodies.py            modify: three tests run for requests too
  test_urllib3_calls.py              create: what differs from httpx
  test_fail_open.py                  modify: the outbound app gains a requests session
```

---

### Task 0: The development dependency

`urllib3` is installed through `requests` already. The new tests import it by name, so it is named.

- [ ] `uv add --group http-clients 'urllib3>=2.0.7'`, then `uv sync`. `pyproject.toml` gains one line in `http-clients` and `uv.lock` records it. 2.0.7 is the oldest version the patch is tested against (Task 3).
- [ ] Run the five commands in Global Constraints. Expected: all pass, nothing else changed.
- [ ] Commit `pyproject.toml` and `uv.lock`: "Name urllib3 as a development dependency for the tests that drive it". The PR body states the reason, as `CLAUDE.md` asks: tests drive a real `urllib3` client.

### Task 1: A call through requests or urllib3 is recorded

**Files:**
- Create: `packages/sdk/src/irimi/_response_recording.py`, `packages/sdk/src/irimi/_urllib3_patch.py`
- Modify: `packages/sdk/src/irimi/_httpx_patch.py`, `packages/sdk/src/irimi/_patching.py`
- Modify: `packages/sdk/tests/conftest.py`, `packages/sdk/tests/test_outbound_calls.py`, `packages/sdk/tests/test_outbound_bodies.py`
- Test: `packages/sdk/tests/test_urllib3_calls.py`

**Interfaces:**
- Consumes: `Interception.begin`, `Interception.end`, `Interception.report_failure`, `OpenCall`, `OutboundCall`, `OutboundResponse`, `CallOutcome`, `Headers`, all unchanged.
- Produces:

```python
# irimi._response_recording, moved from irimi._httpx_patch with only the name changed
class ResponseRecording:
    def __init__(
        self, interception: Interception, open_call: OpenCall, response: OutboundResponse
    ) -> None: ...
    def add(self, chunk: bytes) -> None: ...
    def end(self, was_read_to_end: bool, error: BaseException | None) -> None: ...


# irimi._urllib3_patch
def patch_urllib3(library: ModuleType, interception: Interception) -> bool: ...


# irimi._patching
LIBRARY_PATCHES: dict[str, Patch | None] = {
    "httpx": patch_httpx,
    "httpx2": patch_httpx,
    "urllib3": patch_urllib3,
    "aiohttp": None,
}


# packages/sdk/tests/conftest.py
@pytest.fixture(params=["httpx", "httpx2", "requests"])
def sync_http_library(request: pytest.FixtureRequest) -> Any:
    return importlib.import_module(request.param)
```

Decisions the signatures leave open:

- **The move.** `_ResponseRecording` is cut from `_httpx_patch.py` and pasted into `_response_recording.py` as `ResponseRecording`, with the imports it needs. Nothing else about it changes. `_httpx_patch.py` imports it. Do not import any other private name of `_httpx_patch.py` from the new patch; `getattr(owner, name, None)` is enough for the checks below.
- **What `patch_urllib3` checks.** It returns `False`, replacing nothing, unless all of these hold: `library.HTTPConnectionPool._make_request` is callable, its parameters begin `self, conn, method, url` and include `body` and `headers` (`inspect.signature`); `library.HTTPResponse` has callable `_raw_read`, `_handle_chunk`, `read_chunked`, `close` and `isclosed`; `read_chunked` is a generator function (`inspect.isgeneratorfunction`). Under `urllib3` 1.26 the first and second fail, which is how 1.x ends up in `unpatched`.
- **What it replaces,** each with a wrapper made with `functools.wraps`: `HTTPConnectionPool._make_request`, and on `HTTPResponse`: `_raw_read`, `_handle_chunk`, `read_chunked`, `close`. Replacing on the classes covers `HTTPSConnectionPool`, pools made before `init`, and every client built on them.
- **How a response is tied to its call.** One attribute on the response instance, set through `response.__dict__`, holds that response's state: its `ResponseRecording` and, for Task 2, the chunked reader. A response without the attribute is not recorded and every wrapper passes straight through for it.
- **The waiting slot.** `patch_urllib3` creates one `threading.local()`. The `_make_request` wrapper puts an entry holding the `OpenCall` in it before calling the original and, in a `finally`, puts back whatever was there before. The first `_raw_read`, `_handle_chunk` or `read_chunked` on a response that has no attribute, while an unclaimed entry waits on this thread, claims it: it builds the `ResponseRecording` from that response's status and headers, sets the attribute, and marks the entry claimed so no other response takes it.
- **The `_make_request` wrapper,** `(pool, conn, method, url, *args, **kwargs)`, in order:
  1. Translate the request (below). If that raises: `report_failure("read an outbound request")`, call the original and return its result. Nothing is recorded.
  2. `open_call = interception.begin(call)`. If `None`, call the original and return its result.
  3. Call the original with the slot set as above.
  4. If it raised: when the entry was claimed, `recording.end(was_read_to_end=False, error=error)`; otherwise `interception.end(open_call, CallOutcome(response=None, body=None, was_read_to_end=False, error=error))`. Re-raise unchanged. Catch `BaseException` for this, as `_httpx_patch.py` does.
  5. If it returned and the entry was not claimed, build the `ResponseRecording` from the response and set the attribute.
  6. Then end the call at once in two cases. The response is not an instance of `library.HTTPResponse`: `end(was_read_to_end=False, error=None)`. Or `response.isclosed()` is true or `response.length_remaining == 0`: `end(was_read_to_end=True, error=None)`.
  7. Steps 5 and 6 are guarded with `report_failure("record an outbound response")`. Return the response.
- **Translating the request.** `body` and `headers` are read from `kwargs`; `urlopen` passes both by keyword.
  - `method`: `str(method)`.
  - `url`: when the target does not start with `/`, the target as it is. Otherwise `f"{pool.scheme}://{host}{port}{target}"`, where the host is `pool.host` wrapped in `[` `]` when it contains `:`, and the port is `f":{pool.port}"` unless `pool.port` is `None` or the scheme's default (80 for `http`, 443 for `https`). This is a pure function of scheme, host, port and target.
  - `headers`: each `(name, value)` of `headers.items()`, or nothing when `headers` is `None`; a `bytes` name or value is decoded as latin-1; a header whose value is `library.util.SKIP_HEADER` is left out.
  - `body`: `None` is no body. `str` is `body.encode()`. Anything with a `read` attribute is a stream. Otherwise `bytes(memoryview(body))`, and a `TypeError` from `memoryview` means a stream. This mirrors `urllib3.util.request.body_to_chunks`. `OutboundCall.body` is the bytes or `None` when empty, and `has_streamed_body` is true only for a stream.
- **Translating the response.** `OutboundResponse(status=int(response.status), headers=...)` with each `(name, value)` of `response.headers.items()`, which yields a repeated header once per value.
- **The `_raw_read` wrapper.** Find or claim the recording. Call the original. Add the returned bytes to the recording when there are any. Then, if `response.isclosed()`, `end(was_read_to_end=True, error=None)`. Task 2 adds the error path.
- **The `_handle_chunk` wrapper.** Call the original, add the returned chunk to the recording, return it.
- **The `read_chunked` wrapper** is itself a generator: `yield from` the original, and when it finishes, if `response.isclosed()`, `end(was_read_to_end=True, error=None)`. Task 2 adds the other endings.
- **The `close` wrapper** in this task only calls the original. Task 2 gives it its job.
- **`ResponseRecording` already copies nothing for a call outside a workflow** and already ends once, under a lock. Rely on both.

- [ ] **Step 1: Add `sync_http_library` to `conftest.py`**

Add the fixture from Interfaces directly after `http_library`. Leave `http_library` and its parameters as they are.

- [ ] **Step 2: Run the shared tests for `requests` too**

Eight existing tests have client code that is the same for `httpx` and `requests`. In each, the fixture argument `http_library` becomes `sync_http_library`, and a `with http_library.Client() as client:` block becomes one call on the module. Nothing else in them changes except the two lines named.

In `packages/sdk/tests/test_outbound_calls.py`:

```python
# test_an_outbound_call_is_recorded_as_a_step_of_its_run
        answer = sync_http_library.post(
            f"{upstream.url}/v1/refunds?expand=charge", json={"amount": amount}
        )
        return note_refund(answer.json())
    ...
    request_body = call_start["request"]["body"]
    assert (request_body["encoding"], request_body["content_type"]) == ("text", "application/json")
    assert json.loads(request_body["data"]) == {"amount": 5}

# test_a_call_inside_a_function_outside_any_workflow_is_recorded_without_bodies
        sync_http_library.post(f"{upstream.url}/v1/metrics", json={"cpu": 0.4})

# test_the_value_of_a_credential_header_is_never_recorded
        return sync_http_library.get(
            f"{upstream.url}/charges", headers={name: "sk_live_123", "Idempotency-Key": "k-1"}
        )

# test_a_password_in_the_url_is_never_recorded
        return int(sync_http_library.get(f"{address}/v1/invoices?limit=3").status_code)

# test_a_header_sent_twice_is_recorded_once_with_its_values_joined
        return int(sync_http_library.get(f"{upstream.url}/catalog").status_code)

# test_init_reports_the_libraries_it_patched
    patched = "patched=('httpx', 'httpx2', 'urllib3'), unpatched=('aiohttp',)"
```

The request body assertion in the first test compares the parsed JSON because `httpx` writes `{"amount":5}` and `requests` writes `{"amount": 5}`.

In `packages/sdk/tests/test_outbound_bodies.py`:

```python
# test_a_compressed_response_is_recorded_as_it_arrived
        return sync_http_library.get(f"{upstream.url}/orders").json()

# test_a_response_larger_than_the_limit_is_cut_and_flagged
        return bytes(sync_http_library.get(f"{upstream.url}/export").content)

# test_a_response_with_no_body_has_no_body_recorded
        return int(sync_http_library.request(method, f"{upstream.url}/refunds/re_456").status_code)
```

Every other test in the two files keeps `http_library`.

- [ ] **Step 3: Create `packages/sdk/tests/test_urllib3_calls.py` with the tests for this task**

The whole file is below, under "The new test file". For this task write its imports, `take_calls`, and the first five tests, down to and including `test_a_call_sent_through_a_proxy_is_recorded_under_the_address_it_was_meant_for`.

- [ ] **Step 4: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_outbound_calls.py packages/sdk/tests/test_outbound_bodies.py packages/sdk/tests/test_urllib3_calls.py -v`
Expected: every `[requests]` case and every test in `test_urllib3_calls.py` FAILS, because no `http` event is recorded for them, and `test_init_reports_the_libraries_it_patched` FAILS on the tuples. Every `[httpx]` and `[httpx2]` case passes.

- [ ] **Step 5: Move `ResponseRecording`, write `_urllib3_patch.py`, map it in `_patching.py`**

- [ ] **Step 6: Run the whole suite and the checks**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy && uv run mypy --python-version 3.10 packages/sdk/src`
Expected: all pass.

- [ ] **Step 7: Commit**

`git commit -m "Record a call made through urllib3 or requests as a step of its run"`

### Task 2: How a urllib3 call ends

**Files:**
- Modify: `packages/sdk/src/irimi/_urllib3_patch.py`
- Test: `packages/sdk/tests/test_urllib3_calls.py`

**Interfaces:**
- Consumes: everything Task 1 produced. Produces nothing new outside `_urllib3_patch.py`.

Decisions:

- **A read that raises.** The `_raw_read` wrapper catches `BaseException` from the original, calls `end(was_read_to_end=False, error=error)` and re-raises unchanged. `_raw_read` maps low-level errors to `urllib3`'s own inside itself, so the error recorded is the `urllib3` one.
- **The `read_chunked` wrapper,** complete. It keeps the original's generator on the response's state and delegates with `yield from`:
  - the original finishes: if `response.isclosed()`, `end(was_read_to_end=True, error=None)`;
  - `GeneratorExit`: `end(was_read_to_end=False, error=None)` and re-raise. The app dropped the stream part way, and `urllib3` closes the connection under it at that moment;
  - any other `BaseException`: `end(was_read_to_end=False, error=error)` and re-raise.
- **The `close` wrapper,** complete. If the response has a recording, and its chunked reader is not running (`generator.gi_running` is false, or there is none), `end(was_read_to_end=False, error=None)`. Then call the original, in a `finally`. The `gi_running` test is what Review Focus 1 needs: `_update_chunk_length` closes the response from inside the running reader and then raises, and the error must be what ends the call. `end` on a call that already ended does nothing, so closing a response that was read to its end records nothing more.
- **A response that is dropped.** Nothing to build: `io.IOBase`'s finaliser calls the `close` wrapper for a response that is not closed.
- **Retries, redirects and threads** need no code beyond Task 1. Their tests are written here because they are part of how calls end, and they are expected to pass as soon as they are written.

- [ ] **Step 1: Add the rest of the tests to `test_urllib3_calls.py`**

From `test_a_streamed_response_is_recorded_whole_when_the_stream_ends` to the end of the file below.

- [ ] **Step 2: Run them**

Run: `uv run pytest packages/sdk/tests/test_urllib3_calls.py -v`
Expected: FAIL, each for want of an end event, for `test_a_response_closed_early_is_recorded_as_far_as_it_was_read` (both cases), `test_a_response_dropped_unread_is_recorded_when_it_is_collected` and the `[requests]` case of `test_a_response_that_breaks_off_is_recorded_with_the_error_urllib3_raised`. Its `[urllib3]` case, the stream read to its end, the response with nothing to read, the request that cannot be sent, retries, redirects and threads PASS already. If one of those fails, the fault is in Task 1; fix it there.

- [ ] **Step 3: Implement the endings**

- [ ] **Step 4: Run the whole suite and the checks**

Run: the five commands in Global Constraints.
Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -m "End a urllib3 call when its response ends, breaks off or is closed"`

### Task 3: Fail open, older versions, and the real thing

**Files:**
- Modify: `packages/sdk/tests/test_fail_open.py`
- Modify: `packages/sdk/README.md` only if it names the libraries that are recorded

**Interfaces:**
- Consumes: everything above. Produces nothing new.

- [ ] **Step 1: Extend the outbound app in `test_fail_open.py`**

`OUTBOUND_APP` gains a `requests` session made before `init` and one call through it:

```python
OUTBOUND_APP = """
import os
import httpx2
import requests
import irimi

client = httpx2.Client()
session = requests.Session()
{setup}
import httpx

@irimi.workflow
def look_up_both():
    print(client.get(os.environ["UPSTREAM_URL"] + "/a").text)
    print(httpx.get(os.environ["UPSTREAM_URL"] + "/b").text)
    print(session.get(os.environ["UPSTREAM_URL"] + "/c").text)

look_up_both()
print(irimi.flush())
"""
```

In `test_a_client_made_before_init_and_a_library_imported_after_it_are_both_recorded` the expected output gains a third `{"ok": true}` line and the names gain `"GET 127.0.0.1/c"`. In `test_outbound_calls_reach_the_app_unchanged_whatever_the_store_does` the expected output gains the third line. Keep the workflow's name; renaming it is not this PR's concern.

- [ ] **Step 2: Run them**

Run: `uv run pytest packages/sdk/tests/test_fail_open.py -v`
Expected: PASS if Tasks 1 and 2 are right. A failure here is a bug in an earlier task; fix it there.

- [ ] **Step 3: Run the suite against the oldest supported urllib3**

Run: `uv run --with 'urllib3==2.0.7' pytest packages/sdk/tests`
Expected: all pass. If a test fails only here, the patch depends on something newer than 2.0.7: fix the patch, or raise the lower bound in `pyproject.toml` and in the design note's "Versions", and say which in the PR body.

- [ ] **Step 4: See urllib3 1.x reported and left alone**

Run:

```bash
uv run --with 'urllib3<2' python -c "
import irimi, urllib3
print(urllib3.__version__)
print(irimi.init(store_url='http://127.0.0.1:9'))
"
```

Expected: a 1.26 version, the warning `irimi could not patch urllib3, so calls made through it are not recorded`, and an `InitResult` with `urllib3` in `unpatched`. Report the output in the PR body.

- [ ] **Step 5: Run the real thing**

No test stands in for this. Work in a scratch directory outside the repo.

1. In one terminal: `uv run irimi store` (it listens on `http://127.0.0.1:7717`).
2. Write `upstream.py`: a `http.server.ThreadingHTTPServer` on `127.0.0.1:8808` that answers `POST /v1/refunds` with `{"id": "re_456"}` as gzip with `Content-Encoding: gzip`, answers `GET /v1/events` with three `data: ...\n\n` events as `text/event-stream` sent chunked with a flush after each, and answers `GET /moved` with a 302 to `/v1/charges`, and anything else with `{"ok": true}`. Run it in a second terminal.
3. Write `app.py`:

```python
import irimi
import requests
import urllib3

irimi.init(store_url="http://127.0.0.1:7717")
UPSTREAM = "http://127.0.0.1:8808"


@irimi.workflow
def handle_ticket(ticket_id):
    session = requests.Session()
    refund = session.post(
        f"{UPSTREAM}/v1/refunds", json={"ticket": ticket_id}, headers={"X-Api-Key": "not-a-key"}
    ).json()
    with session.get(f"{UPSTREAM}/v1/events", stream=True) as events:
        lines = [line for line in events.iter_lines() if line]
    moved = session.get(f"{UPSTREAM}/moved").status_code
    charge = urllib3.request("GET", f"{UPSTREAM}/v1/charges/ch_1").json()
    return refund, lines, moved, charge


print(handle_ticket("T-1042"))
print(irimi.flush())
```

4. Run it with the workspace's environment: `uv run --project <path to the worktree> python app.py`. Then `uv run irimi runs --store-url http://127.0.0.1:7717` and `uv run irimi runs <id> --json --store-url http://127.0.0.1:7717`.

Expected: the script prints what it would print without irimi (the refund, three event lines, `200`, `{'ok': True}`) and `True`. The run lists five `http` children in order: `POST 127.0.0.1/v1/refunds`, `GET 127.0.0.1/v1/events`, `GET 127.0.0.1/moved`, `GET 127.0.0.1/v1/charges`, `GET 127.0.0.1/v1/charges/ch_1`. Read two of the calls in full from the store's call read, `GET /api/v1/runs/<run id>/calls/<span id>`: the refund's request has `X-Api-Key` as the redaction marker and its response body is `base64` with `content_encoding: gzip`; the events call's response body holds all three events and has no flags. Report what was seen in the PR body. If any of it differs, stop and fix before opening the PR.

If #18 has merged into `main` and this branch is rebased on it, the children are still `http`: none of these hosts is a provider's.

- [ ] **Step 6: Run the whole suite and the checks, then commit and open the PR**

Run the five commands in Global Constraints. `git commit -m "Cover urllib3 calls when the store is away and a session predates init"`, then `gh pr create` against `main` with `Closes #19`, the reason for naming `urllib3` as a development dependency, the departures from the design note in a few lines, and what Steps 3, 4 and 5 showed.

---

## The new test file

`packages/sdk/tests/test_urllib3_calls.py`, whole. Tasks 1 and 2 say which part each writes.

```python
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
```
