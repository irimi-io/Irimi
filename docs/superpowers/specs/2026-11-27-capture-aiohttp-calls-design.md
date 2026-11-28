# Piece 4, PR 4: Capture calls made through aiohttp

Design note for issue #20. Written 2026-11-27. It fixes the patch target for `aiohttp` and says
where the library departs from the `httpx` behaviour, as "Delivery" in
`docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md` (the piece 4 spec) asks.
Everything the piece 4 spec says holds unless this note names a departure.

Every choice here is **proposed** (the author's call; Henry was away) and stays inside what the
piece 4 spec's "Needs an OK" already covers: no wire-format addition, no new flag or field, no
public-surface change beyond `aiohttp` appearing in `InitResult.patched`, no SDK dependency.

What is said about `aiohttp` was read from the installed source, version 3.14.5
(`client.py`, `client_reqrep.py`, `client_middlewares.py`, `streams.py`, `http_parser.py`,
`client_proto.py`, `payload.py`, `connector.py`), and tried with a throwaway script against
3.14.5 and 3.13.0 on the loopback address.

## The patch target

**`ClientSession._request` is wrapped for one purpose: to add a recording client middleware as
the innermost middleware of every request. The middleware is the point where a call is
recorded.**

How a request travels in `aiohttp`:

- `ClientSession.request`, `get`, `post` and the other verbs, `ws_connect` (through
  `self.request`) and the module-level `aiohttp.request` all call `ClientSession._request`.
  Nothing sends a request without it.
- `_request` holds the redirect loop and the one retry of an idempotent request on a dropped
  connection. Inside the loop it builds a `ClientRequest` for the hop, then calls a handler with
  it. The handler is the local function `_connect_and_send_request` (take a connection, call
  `ClientRequest.send`, call `ClientResponse.start`, which reads the status line and headers),
  wrapped by the session's client middlewares, or by the ones passed as `middlewares=` for that
  request. `build_client_middlewares` makes the last middleware in the list the innermost.
- So a middleware appended last sits directly around "connect, send, read the headers", once per
  request on the wire: once per redirect hop and once per retry, after the app's own middlewares
  (digest auth, for one) have changed the request, and before anything is sent. That is the same
  place the `httpx` transport is: fork A1 of the piece 4 spec. In piece 5 the same middleware
  can answer without calling the handler.

The wrapper around `_request` does nothing else. It reads the middlewares in effect
(`middlewares=` if given, else `session._middlewares`), appends the recording middleware and
calls the original with the same arguments. If that step fails, the failure is counted and the
request is sent unrecorded.

**Why not `ClientRequest.send`,** the candidate in the piece 4 spec:

- It runs after the connection is taken, so a request that cannot connect never reaches it and
  would not be recorded. Through `httpx` that call is recorded with its error.
- It returns a response that has no status and no headers yet. They are read by
  `ClientResponse.start`, a second method that would need its own patch.
- The connector calls `send` and `start` itself for the `CONNECT` request to an HTTPS proxy
  (`connector.py`, `_create_proxy_connection`). That is not a request the app made.

**Supported versions.** The patch needs three things: the `middlewares` parameter of `_request`,
`BytesPayload.as_bytes` and `StreamReader.total_compressed_bytes`. Of the versions probed
(3.11.18, 3.12.0, 3.12.15, 3.13.0, 3.14.0, 3.14.5), all three are present from 3.13.0 on. An
older `aiohttp` goes in `unpatched` with the one warning, as the piece 4 spec says for a missing
target.

## How a call is recorded

The patch translates and nothing more. Naming, payloads, credential headers, flags, the body
limit and the place in the run are the existing code in `_interception.py`, `_outbound_calls.py`
and `_bodies.py`.

**The request,** read in the middleware before the handler is called:

- `method` from `request.method`, `url` from `str(request.url)` (query included; `aiohttp` has
  already moved any `user:password@` into an `Authorization` header), `headers` from
  `request.headers.items()`.
- `body`: the bytes of `request.body` when it is a `BytesPayload`, read with `as_bytes()`, which
  returns the bytes it holds without consuming anything. That covers `data=` given bytes or a
  string, `json=`, and a URL-encoded form. Any other payload (a file, an async generator,
  another response's stream, a multipart form) is a streamed body: not read, flagged
  `request_body_not_recorded`.

**The response,** when the handler returns: `status` from `response.status`, `headers` from
`response.raw_headers` decoded as latin-1, the same as the `httpx` patch.

**The body.** `response.content` is an `aiohttp.StreamReader`. Every way an app reads a body
goes through it: `response.read()`, `text()` and `json()` call `content.read()`; streaming code
calls `content.read(n)`, `readany`, `readline`, `readuntil`, `readchunk`, `readexactly`,
`read_nowait`, or iterates with `async for`, `iter_chunked`, `iter_any` or `iter_chunks`, which
are built on those. The patch replaces `response.content` with a recording reader that holds the
original and passes every call to it. A read also copies what it returns, up to the size limit.

- The recording reader is a subclass of the library's `StreamReader`, built with `type()` when
  the library is patched, as the `httpx` stream classes are. `aiohttp` picks how to send a
  request's `data` by its class (`payload.py` registers `StreamReaderPayload` for
  `StreamReader`), so a response's stream passed on as another request's body must still be
  one.
- The parser and the connection keep feeding the original reader. `StreamReader` has
  `__slots__`, so a method cannot be replaced on one instance, and the object the parser holds
  cannot be swapped. That leaves wrapping.
- The recording reader does not run `StreamReader.__init__`, so no inherited method may run on
  it except the four iterator factories, which only call the public read methods. At patch time
  the patch checks that every public name `aiohttp`'s `StreamReader` defines is one the
  recording reader handles. If a newer `aiohttp` has a name it does not know, the library is
  left unpatched with the warning, instead of an `AttributeError` reaching the app later.

**When the call ends,** once, on the first of:

1. The headers arrive and no body follows: `response.content.at_eof()` is already true (a
   `HEAD`, a 204 or 304, an empty body), or the status is 101. Whole.
2. A read returns and the reader is at its end. Whole.
3. A read raises. `error`, with what earlier reads returned, flagged `response_body_incomplete`.
4. The response is closed or released before its end. `aiohttp` then calls `set_exception` on
   `response.content` (`ClientResponse._notify_content`), which is where the recording reader
   sees it. `ok`, flagged `response_body_incomplete` unless the event-stream rule applies.

A response that is never read to its end and never closed has no end event, as with `httpx`.
"At its end" is `at_eof()`: the body's end has arrived and nothing is left to read. An app that
reads an exact count of bytes from a chunked body and closes without one more read can close
before the final chunk has arrived; that call is flagged `response_body_incomplete`.

## Departures from the httpx behaviour

1. **A compressed response is recorded decoded** (known and accepted in the piece 4 spec).
   `aiohttp` decodes `gzip`, `deflate`, `br` and `zstd` before client code sees the bytes. The
   body is recorded as the app read it, with no `content_encoding`, while the recorded
   `Content-Encoding` header still says what was sent. Whether a body was decoded is read from
   the reader (`total_compressed_bytes` is set only when `aiohttp` decodes), not guessed from
   the header, so a session with `auto_decompress=False` is recorded as `httpx` records:
   compressed, as base64, with `content_encoding`.
2. **A whole-body read that fails records no body.** `response.read()` gathers the body inside
   `aiohttp` and raises without handing over what had arrived. The app got nothing, so nothing
   is recorded; the call ends with the error, the response status and
   `response_body_incomplete`. An app that reads in pieces has the pieces it read recorded.
3. **A redirect answer's body is not recorded.** `aiohttp` follows redirects inside the session
   and releases each redirect answer without reading it. Each hop is still its own call, as
   with `httpx`, with the request body on each hop that sends one. A hop whose answer carried a
   body ends `ok`, without a body, flagged `response_body_incomplete`.
4. **An error raised above the middleware is not the call's error.** `raise_for_status`,
   `TooManyRedirects` and a bad redirect address are raised by the session after the call has
   ended. The call's `status` stays `ok`, as approved item 9 says for any HTTP status.
5. **Some request headers are added after the start event is written.** `ClientRequest.send`
   adds `Content-Type: application/octet-stream` to a `POST`, `PUT` or `PATCH` that has none,
   and a `Connection` header in two cases. The connector adds proxy headers. They are sent and
   not recorded. `Host`, `User-Agent`, `Accept`, `Accept-Encoding`, `Content-Length`, `Cookie`
   and `Authorization` are set earlier and are recorded.
6. **A request body sent with `compress=` is not recorded.** `aiohttp` compresses it while
   writing, so the bytes it holds are not the bytes sent, and its `Content-Encoding` header
   would describe them wrongly. The call is flagged `request_body_not_recorded`.
7. **A websocket handshake is recorded as an `http` call that ends at the 101.** Its request and
   response headers are recorded. Nothing after the handshake is recorded: websockets are a
   non-goal of piece 4. A handshake the server refuses is an ordinary call.
8. **There is no sync client.** Every call is made on an event loop, and the middleware runs in
   the task that made the call, so the current call is found as it is for `httpx`'s async
   client.
9. **Bytes pushed back are copied twice.** `StreamReader.unread_data`, deprecated in `aiohttp`,
   puts bytes back to be read again, and they are recorded again.

Not departures, checked: a multipart form and a body given as an iterator are not recorded
through `httpx` either; each redirect hop and each retry is one call in both; the event-stream
rule and the size limit are the shared code.

## What is not recorded

- A session whose `_request` is replaced by something else after `init`, such as a test
  library's mock. Nothing reports it.
- The `CONNECT` request to an HTTPS proxy, and proxy headers.
- Websocket messages.
- A call made through an `aiohttp` older than 3.13.0, or newer than the patch knows.
  `InitResult.unpatched` reports it.
- The server side (`aiohttp.web`). It is not patched.

## Changes to shared code

Kept small, because #18 and #19 change the same files.

- `_outbound_calls.py`: `OutboundResponse` gains `is_body_decoded: bool = False`. When it is
  true the body is described without `content_encoding`, and the event-stream rule treats the
  body as readable whatever the `Content-Encoding` header says. Nothing on the wire changes.
- `_httpx_patch.py`: `_ResponseRecording` and `_end_without_response` have a second caller, so
  they move, unchanged, to `_response_recording.py` as `ResponseRecording` and
  `end_without_response`. #19 is likely to need the same move; whichever merges second uses
  the module that is there.
- `_patching.py`: `"aiohttp": patch_aiohttp`.

## Tests

- A new `packages/sdk/tests/test_aiohttp_calls.py`, through `irimi.init` and a real
  `aiohttp.ClientSession` against the existing `FakeUpstream`. The existing outbound tests
  drive the `httpx` API (`Client`, `stream`, `iter_raw`), which `aiohttp` does not share, so
  they are not parametrised with it. The websocket test uses an `aiohttp.web` server on the
  loopback address.
- One test per behaviour the patch owns: a call as a step of its run; every way of reading a
  body (parametrised); decoded and undecoded compressed bodies; request bodies in memory and
  streamed (parametrised); redirect hops; an early close; the event-stream rule on a decoded
  body; a response that breaks off; a request that cannot connect; no body; a call outside a
  workflow; credentials; many streams at once; the app's own middlewares; a websocket.
- Shared logic is not tested again per library: the credential list, joined headers, the
  cut at a character boundary.
- Where cases differ only in the library, the existing tests take it as a parameter: the two
  subprocess tests in `test_fail_open.py` (a client made before `init`; the app unchanged
  whatever the store does), and the expected `InitResult` in `test_outbound_calls.py`.
- A subprocess test that an `aiohttp` whose `StreamReader` has a public name the patch does not
  know is reported in `unpatched` and still works.
- Before the PR is called done: the test file is run once against `aiohttp` 3.13.0, and a real
  `aiohttp` client is run against a local fake upstream and a local `irimi store` and read back
  with `irimi runs`.

## Risks

- **`_request` and `_middlewares` are private names.** Limit: the patch checks the `middlewares`
  parameter at patch time and leaves the library unpatched without it; reading
  `session._middlewares` is guarded and counted. A renamed attribute means calls are not
  recorded and `flush` reports drops, not that the app breaks.
- **A newer `aiohttp` adds a public name to `StreamReader`.** The library goes to `unpatched`
  until the SDK knows the name. That is chosen over the alternative, an inherited method running
  on a reader that was never initialised.
- **A read path that does not go through `response.content`.** None exists in 3.14.5. If one is
  added, bodies read through it are not recorded and the call ends at close, flagged
  incomplete.
- **Work on the event loop.** A copy of each chunk, up to 4 MB per body, and one extra call per
  read. Nothing is decoded or parsed.
- **Piece 5.** To answer a call without sending it, the middleware must return a
  `ClientResponse` that has no connection. That is for piece 5 to design; this note only puts
  the decision point where it can be done.
- **Merges.** #18 and #19 touch `_outbound_calls.py`, `_patching.py`, `_httpx_patch.py` and
  two tests this PR also touches. Each change here is a few lines.

## Needs an OK

Nothing beyond what was approved on 2026-11-27. Three choices in this note are the author's and
worth a look: `aiohttp` before 3.13.0 is left unpatched; a request body sent with `compress=` is
flagged `request_body_not_recorded`, a flag the piece 4 spec describes for streamed bodies; a
newer `aiohttp` with an unknown `StreamReader` name is left unpatched.
