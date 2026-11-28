# Piece 4, PR 3: Capture calls made through urllib3

Design note for issue #19. Written 2026-11-27. It fixes the patch target for `urllib3` and says
where the library departs from the `httpx` behaviour, as "Delivery" in the piece 4 design asks
(`docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md`, called "the parent"
below).

Everything the parent decided stands: what is recorded for one call, the flags, the credential
list, where a call sits in a run, failing open. This note adds nothing to the wire format, nothing
to the public SDK surface beyond `urllib3` moving from `unpatched` to `patched`, and no SDK
dependency. Every choice below is **proposed** (the author's call, open to correction) unless it
cites the parent.

**Read for this note,** in the installed packages, not from memory: `urllib3` 2.8.0
(`connectionpool.py`, `response.py`, `connection.py`, `poolmanager.py`, `http2/connection.py`),
`requests` 2.34.2 (`adapters.py`, `models.py`), and for the version question `urllib3` 1.26.20,
2.0.7 and 2.2.3. The design was tried as a throwaway patch against 2.0.7, 2.2.3 and 2.8.0 with
`requests` 2.34.2, and every test named in the plan passed against it.

## Intent

After `irimi.init()`, a call made through `urllib3`, and so through `requests`, is recorded as a
call of its run the way an `httpx` call is: one recorded call per request on the wire, its request
at the start, its response when the body ends.

## The patch target

**`HTTPConnectionPool._make_request`**, with four methods of `HTTPResponse` to follow the body.

`_make_request` is the one place every request on the wire passes through. `urllib3.request`,
`PoolManager`, `ProxyManager` and `requests`' adapter all end in `HTTPConnectionPool.urlopen`, and
`urlopen` calls `_make_request` exactly once per attempt. `HTTPSConnectionPool` and the SOCKS
pools are subclasses that do not override it. It is called with the method, the target, the
headers and the body before anything is sent, and it returns that attempt's response before
`urlopen` decides to retry or redirect.

Not picked:

- **`HTTPConnectionPool.urlopen`,** the parent's candidate. It is public, but it is not one call
  per attempt in a way a patch can use: it calls itself for each retry and redirect, and it reads
  and discards a redirect's or a retried response's body before returning the last response. A
  wrapper around it sees nested calls and never sees the earlier responses.
- **`HTTPConnection.request` and `getresponse`.** Public, but two calls that have to be paired on
  the connection, the connection does not know the scheme, behind a proxy its host is the proxy's,
  and `HTTP2Connection` overrides both.
- **`HTTPResponse`'s public `read`, `read1` and `stream` only.** They return the body after
  `urllib3` has decompressed it, so recording there would lose the bytes as they arrived and would
  need the SDK to work out whether each body was decoded.

`_make_request` is a private name, where the parent's rule (fork A, option 1) says "lowest public
call". No public call in `urllib3` is one request on the wire. The limit is the one the parent
already sets for a library that changes: the patch checks its targets when it is applied, and
when one is missing or has an unexpected shape `urllib3` is reported in `unpatched` with the one
warning.

### Following the response

`httpx` hands the patch a byte stream it can wrap. `urllib3` has none: an `HTTPResponse` reads
its socket itself, in two places. So the patch replaces these on `HTTPResponse`:

| Method | Why |
| --- | --- |
| `_raw_read` | Every read that is not a chunked stream goes through it: `read`, `read1`, `readinto`, `data`, `stream` on a sized body, `drain_conn`, and the read `urllib3` does itself when `preload_content` is true. It returns the bytes as they arrived, before decompression. |
| `_handle_chunk` | Returns each chunk of a chunked body as it arrived. `stream` and `read_chunked` use it, so `requests` does for a chunked answer. |
| `read_chunked` | Where a chunked stream finishes, fails, or is abandoned. |
| `close` | Where the app gives up on a response. |

A response is tied to its call by one attribute set on the response object.

**A response `urllib3` reads before returning it.** With `preload_content` true, which is the
default for `urllib3` used directly, the whole body is read inside `HTTPResponse.__init__`, inside
`_make_request`, before the patch has the response in hand. So while the original `_make_request`
runs, the open call waits in a thread-local slot, and the first read by a response that has no
call yet takes it. `urllib3` is synchronous, so the response built during a `_make_request` is
built on its thread. The slot is held by the patch, not by the module, and a `_make_request`
entered while another is running on the same thread (a request body generator that makes its own
request) puts the outer entry back when it returns.

### Versions

The five targets exist with the same shape in every 2.x release looked at (2.0.7, 2.2.3, 2.8.0).
`urllib3` 1.26 is built differently: `_make_request` returns the standard library's response, the
`HTTPResponse` is made later in `urlopen`, and there is no `_raw_read`. Covering it would be a
second patch. It is not covered: under 1.x the check fails, `urllib3` is in `unpatched`, and the
warning is logged. `requests` 2.34.2 still allows 1.26, so an app that pins it gets that report
and no recording. The oldest version the patch is tested against is 2.0.7.

## Departures from the httpx behaviour

1. **When a call ends.** An `httpx` call ends when its wrapped stream is exhausted, raises or is
   closed. A `urllib3` call ends on the first of:
   - a read leaves the response finished (`isclosed()` is true after `_raw_read`, or
     `read_chunked` returns): whole;
   - a read raises: `error`, with the response and what was read;
   - `close()` before the end: `ok`, flagged `response_body_incomplete`;
   - a chunked stream's generator is dropped before the end, which is when `urllib3` closes the
     connection under it: `ok`, flagged `response_body_incomplete`;
   - `_make_request` returns a response with nothing to read (`length_remaining == 0`: a `HEAD`,
     a 204, a 304, `Content-Length: 0`): whole, at once, whether or not the app reads it;
   - `_make_request` raises: `error`, without a response.

   The parent's rule for an event stream closed right after a whole event applies unchanged,
   because it is decided in the shared code from the bytes recorded.

2. **A response the app drops without closing is ended when it is collected.** `HTTPResponse` is
   an `io.IOBase`, whose finaliser calls `close()` on an object that is not closed. So a response
   that was neither read to the end nor closed gets its end event, flagged
   `response_body_incomplete`, when the garbage collector frees it. With `httpx` such a call has
   no end event.

3. **Compressed bodies: no departure.** `urllib3` and `requests` hand the app a decompressed
   body, but the patch copies below that, so a gzip response is recorded as gzip with
   `content_encoding: gzip`, as the parent's "A compressed response" says. This is unlike
   `aiohttp`.

4. **A response read whole before the app has it.** With `preload_content` true the call has
   already ended when `urllib3.request` returns. When that read fails part way, the standard
   library raises without handing over the bytes it had, so the end event has the response's
   status and headers, the error and `response_body_incomplete`, but no body.

5. **Retries.** `urllib3` retries inside `urlopen` (three times by default when used directly,
   none by default under `requests`). Each attempt is its own recorded call, as each hop of a
   redirect is. An attempt that failed is a call with `status: error`. The parent's non-goal
   "retries as one logical call" stands.

6. **The error recorded is the one the attempt raised.** The patch sits below the code that wraps
   errors, so a refused connection is recorded as `urllib3.exceptions.NewConnectionError` and a
   broken body as `urllib3.exceptions.ProtocolError`, while the app sees
   `urllib3.exceptions.MaxRetryError` or `requests.exceptions.ConnectionError` or
   `ChunkedEncodingError`. The app's exception is unchanged. With `httpx` the two are the same
   type.

7. **Redirects.** No departure in what is recorded: one call per hop, each with its request body,
   and the redirect response's own body, which `urllib3` and `requests` both read before following.
   `requests` follows redirects by default and `httpx` does not, which changes how many calls an
   app makes, not how one is recorded.

8. **The request body.** `bytes`, `bytearray`, `memoryview` and `str` are in memory and recorded;
   a `str` is recorded as the UTF-8 bytes `urllib3` sends. A file-like object or any other
   iterable is a stream: not read ahead of the library, not recorded, flagged
   `request_body_not_recorded`. `requests` builds JSON, form and multipart bodies in memory before
   it calls `urllib3`, so those are recorded.

9. **Request headers are the ones handed to the pool.** `urllib3` and the standard library add
   headers while sending: `Host`, a default `User-Agent`, `Accept-Encoding: identity`, and the
   `Content-Length` or `Transfer-Encoding` they work out for the body. Those are below the patch
   and are not recorded. Under `requests` most are already set by `requests` and so are recorded;
   `Host` never is. A header whose value is `urllib3.util.SKIP_HEADER` is an instruction not to
   send it and is left out. With `httpx` the recorded headers are all the headers sent.

10. **The URL is put together from the pool.** `_make_request` gets a target, not a URL. When the
    target is a path, the URL recorded is the pool's scheme and host, the pool's port unless it is
    the scheme's default, and the target. So a URL written as `https://host:443/x` is recorded as
    `https://host/x`, and the host is in the lower case `urllib3` keeps it in. When the target is
    absolute, which is how a request goes to a plain HTTP proxy, it is recorded as it is, so a
    call through a proxy is named for the host it was meant for. A path target never has a
    `user:password@` part, and `requests` turns one into the `Authorization` header before the
    pool is reached; where an absolute target carries one, the shared code removes it as it does
    for `httpx`.

## What is not recorded

- Anything, under `urllib3` 1.x.
- A request made by a pool class that overrides `_make_request`, or the body of a response whose
  class overrides the four methods. As with an `httpx` client given its own transport, nothing
  reports it.
- The body of a response that is not an `HTTPResponse`: `urllib3`'s experimental HTTP/2 support
  and its Emscripten support return other classes. The call is recorded with its request, status
  and headers, ends at once, and is flagged `response_body_incomplete`, the flag the SDK already
  uses when it could not copy a body.
- Headers added below the pool, a streamed request body, and the bytes of a preloaded read that
  failed (departures 9, 8 and 4).
- An error `urllib3` raises while decompressing. The bytes arrived whole, so the call ends `ok`
  with the body as it arrived; the app gets its `DecodeError`.
- Calls through a copy of `urllib3` that another package carries inside itself, such as `pip`'s.

## Shared code

One piece of logic still decides how a call is recorded: the patch builds an `OutboundCall`, an
`OutboundResponse` and a `CallOutcome` and hands them to `Interception`, as the `httpx` patch
does. `_outbound_calls.py`, `_interception.py` and `_bodies.py` do not change.

- `_ResponseRecording` (the copy of a body and the once-only end) has a second caller, so it moves
  unchanged from `_httpx_patch.py` to `irimi/_response_recording.py` as `ResponseRecording`.
- `_patching.py` maps `urllib3` to the new patch.
- The new code is one module, `irimi/_urllib3_patch.py`.

Issues #18 and #20 are being built at the same time. The lines they are likely to touch too are
the `LIBRARY_PATCHES` entries, the move of `_ResponseRecording`, and the expected text in
`test_init_reports_the_libraries_it_patched`.

## Tests

- The existing tests whose client code is the same for `httpx` and `requests` (`get`, `post`,
  `request`, `status_code`, `json()`, `content`, `headers`) run against all three through a new
  fixture, `sync_http_library`: one call as a step of its run, a call under a function outside a
  workflow, credential headers, a password in the URL, a repeated header, a compressed response,
  a response over the limit, a response with no body.
- What differs from `httpx` gets its own tests in `test_urllib3_calls.py`, through real
  `requests` and `urllib3` clients against the local upstream: request bodies in memory and
  streamed, the headers recorded, a preloaded compressed response, a call through a proxy, a
  stream read to its end, a response closed early (chunked and sized, the two ways it ends), a
  response dropped unread, a response with nothing to read, a body that breaks off (streamed and
  preloaded), a request that cannot be sent, retries, redirects, and calls on several threads at
  once.
- The subprocess tests for failing open gain a `requests` session made before `init`.
- `urllib3` becomes a named development dependency, because tests import it; it is already
  installed through `requests`.
- Before the PR is called done: a script with real `requests` and `urllib3` clients against a
  local upstream and a local `irimi store`, read back with `irimi runs`; the suite once more
  against `urllib3` 2.0.7; and `init` once against `urllib3` 1.26 to see it reported.

## Risks

- **Three private names** (`_make_request`, `_raw_read`, `_handle_chunk`). They have had this
  shape since 2.0, and `urllib3` may change them in any release. Limit: the check when the patch
  is applied, the `unpatched` report, and every added step failing open.
- **`urllib3` 1.x is seen and not patched.** An app pinned to it is told so once, at `init`.
- **The waiting slot can be taken by the wrong response** in one case: a response made before
  `init`, first read from inside the body generator of a later request on the same thread. That
  request's call would get the older response. Not guarded.
- **An end written from a finaliser.** It runs on whatever thread collects the response, possibly
  as the interpreter exits. It goes through the same guarded path as any end, and one that cannot
  be sent is counted as dropped.
- **Work on the app's thread and memory,** as in the parent: a copy of up to 4 MB per response
  being read.

## Needs an OK

Nothing. The note stays inside what Henry approved on 2026-11-27: no wire addition, no change to
the public surface, no SDK dependency. Two choices are worth his eye all the same, because they
are the author's call and the parent pointed elsewhere: the patch target is a private method, not
the parent's candidate `urlopen`; and `urllib3` 1.x is left unpatched.
