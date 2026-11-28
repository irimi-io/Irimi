# Piece 4: Capture HTTP and LLM calls

Design for the fourth of the six pieces of the Step 2 build. Written 2026-11-27, revised the same
day after a review by fresh readers.

Each choice below is marked **decided** (Henry said so), **from Notion** (already fixed in Step 1
or Design Decisions) or **proposed** (the author's call, open to correction). The proposals that
change the wire format, the public SDK surface or something already decided are collected under
"Needs an OK".

**Sources.** "From Notion" means three pages under the Irimi page: Scope, Step 1: Decide the
boundaries, and Design Decisions. The project rules are `CLAUDE.md` at the repo root. Piece 3's
design is `docs/superpowers/specs/2026-11-26-read-a-run-design.md`. Braintrust is the tracing
product Scope compares against; its Python SDK is `braintrust-sdk-python` on GitHub.

## Intent

Today the SDK records only what the user decorated: the workflow and its labeled functions. Model
calls and outbound HTTP calls, which a replay answers and a diff compares, are not recorded.
Piece 4 records them with no labels, after `irimi.init()` alone. Piece 5 (replay) answers calls
at the same point this piece records them, and piece 6 (`irimi diff`) prints what was recorded.

## The system today

A **run** is one execution of a function marked `@irimi.workflow`. On the wire a run is a trace,
and each **call** in it (the workflow, a labeled function, and after this piece an outbound call)
is a span: a start **event** and an end event, sent to the **store**. Each call has a parent and
a `seq`, its order within the run. The SDK's recorder tracks the **current call**, so a new call
knows its parent, and hands events to the exporter, which sends them in batches from a background
thread. An event can carry `flags`, a list of words such as `unattached`. What the SDK fails to
record or send is counted, and `flush` reports it. Readers get a run back as a tree through the
store's reader API, which `irimi runs` prints.

## Goals

- After `irimi.init()`, an outbound HTTP call made through a patched library's own transport is
  recorded as a call in the run it happens in, with its request and response. The Flags list
  names what is left out of a body. (from Notion)
- A call to one of four LLM providers (Anthropic, OpenAI, Gemini, OpenRouter), at the paths in
  the recognition table, is recorded as an `llm` call, named by provider and operation without
  the model. (decided 2026-11-27, replacing Scope's one-provider limit)
- A streamed response is recorded as it is read (from Notion), and the call ends when the stream
  ends (proposed, from Braintrust).
- The value of a header on the credential list is never recorded. Other parts of a call are
  recorded as the Design section describes. (decided 2026-11-27)
- `InitResult.patched` and `unpatched` say what `init` patched, and what it saw and did not
  patch. (from Notion)
- The point that records a call is the one point piece 5 will decide at, whichever library the
  call came through. (from Notion: one piece of logic decides how a call is replayed)

## Non-goals

- Redaction: the `redact=` hook, credentials in URL parameters and in body fields. Picked up with
  hosting. (decided 2026-11-27)
- Answering a call from a recording, faking a write, overriding the model, the replay proxy, the
  guard against unpatched database drivers. All piece 5. (from Notion)
- Reading the model, token usage or the model's output out of a body. See fork B. (proposed)
- Provider SDK or agent framework patching. (from Notion)
- Thread tracking. A call made on a thread that lost its run is recorded as unattached.
  (from Notion)
- Websockets, gRPC, signed requests, retries as one logical call. (from Notion, "Later: beyond
  HTTP interception")
- Gemini through Vertex AI, Azure OpenAI and any custom base URL. They are recorded as plain
  `http` calls. In a replay such a call is treated as `http`: served from the recording or faked,
  not sent to the other model. Piece 5 has to say so to the user or recognise more. (proposed)
- Flagging a run in which a `write=True` function makes an LLM call. A reader can see it in the
  tree, as an `llm` call under a call marked as a write. (proposed)
- The "contains redacted values" condition on a run. It comes with the `redact=` hook; until
  then every run with an LLM call would carry it. (proposed)
- Making the store's reads cheap for large events. See "The parts and who owns what". (proposed)

## Anti-goals

- A recording step that changes what the app sees: a different response, a consumed stream, a
  changed header, an exception that came from the SDK.
- A patch that raises at `init` or at call time because a library is missing or is a version the
  SDK did not expect.
- A second place that knows how a call is recorded. Each library patch translates and nothing
  more.
- The value of a listed credential header in the store.

## Constraints

- `CLAUDE.md` in full. In particular: the SDK has no dependencies and none is added; in a normal
  run it fails open, never raises into the app and never blocks it; what it drops is counted.
- The SDK supports Python 3.10 and passes `mypy --strict` on it. The libraries it patches are not
  installed in every app, and `init` must not fail when one is absent.
- A change to the wire format needs an explicit OK. This piece adds fields and flags; they are
  listed under "Needs an OK".
- Tests use no network and no API keys.
- Wire shapes use only values every language can read. (decided 2026-11-27, piece 3)

## What was learned from Braintrust

Source read 2026-11-27 (`braintrust-sdk-python`, commit `7fd4377`). "Taken" means adopted in this
design.

- It does not intercept HTTP. It patches each provider's client class. Its Anthropic integration
  alone is about 2,100 lines that follow that SDK's surface version by version, a cost the HTTP
  level does not have.
- **Patch the class, in place, once.** Each integration names its library and its patch targets
  and marks what it patched, so a second call does nothing. Taken.
- **Setup never raises.** A missing library is skipped silently, an unsupported one warns and is
  skipped, and the result says which took. Taken, as `patched` and `unpatched`.
- **A streamed call ends when the stream ends.** The stream wrapper passes each chunk through
  untouched, keeps a copy, and ends the span exactly once on the first of: exhaustion, an error,
  close. Taken.
- It uses `wrapt` to patch. Not taken: plain attribute replacement keeps the SDK at no
  dependencies.

## Approaches

Two forks, A and B. Options are numbered within each.

### Fork A: where a library is patched

1. **Each library's own lowest public call that sends one request.** For `httpx` and `httpx2`
   that is `HTTPTransport.handle_request` and `AsyncHTTPTransport.handle_async_request`. One
   recorded call is one request on the wire, so a redirect or a client's retry is several calls.
   A client that brings its own transport class is not covered by this patch.
2. **Each library's top-level client call** (`Client.send`). One recorded call per call the app
   made, with redirects folded in. It sits above the client's auth and redirect handling, so in
   piece 5 a faked answer would skip logic the app relies on, and a library layered on another
   (Anthropic's optional aiohttp transport under an httpx client) would be recorded twice.
3. **The standard library's connection classes,** under everything. Covers libraries the SDK
   never named. It sees bytes on a socket, not a request the app made, and does not cover
   `httpx`, which has its own HTTP implementation.

**Picked: option A1** (proposed). At the `httpx` transport a request has been through the
client's auth and redirect handling and nothing has been sent, which is what piece 5 needs. The
rule for the other libraries is the same; whether each can follow it is settled in that
library's PR.

### Fork B: where provider knowledge is applied

Recording needs to know that a request is an LLM call (to set `kind` and `name`). Reading the
model, the token usage and the model's text out of a body needs to know each provider's body and
stream format.

1. **All in the SDK at record time.** Matches the worked example in Step 1, which shows
   `gen_ai.request.model` on the start and `usage` on the end. The SDK parses every LLM body on
   the app's thread, every later SDK language repeats the parsing, and a recording made before a
   parsing fix stays wrong.
2. **The SDK recognises, later pieces read.** At record time the SDK only matches host and path
   against a table to set `kind` and `name`, and records the body as it is. Whoever needs the
   model or the usage reads it from the recorded body: piece 5 to swap the model, piece 6 to show
   cost. The body is recorded up to the size limit, so a reader that is fixed later can read old
   recordings too. A cut, incomplete or unrecorded body cannot be read, and in a stream the usage
   arrives last, so a cut stream loses it. The per-provider reading is still written, in pieces 5
   and 6.
3. **All in the store.** The start event needs `kind` and `name` when it is written, so
   recognition cannot wait for the store. Dropped.

**Picked: option B2** (proposed, and a departure from the worked example: see "Needs an OK"). It
keeps this piece to recording, and in this piece a provider is rows in one table.

## Design

### The parts and who owns what

```
app code
   │  httpx / httpx2 / urllib3 / aiohttp
   ▼
library patch ──► outbound call ──► the one interception point ──► Recorder ──► Exporter ──► store
(one per library,   (plain data:     (records now; piece 5
 translates only)    method, URL,     decides here)
                     headers, body)
```

All of the recording is in the SDK package, and no store or CLI document shape changes: the store
keeps and returns fields it does not know, and the call read already returns a call with
everything on it. The store's reads do change in cost. The tree read, the run list and the replay
read load each event whole and page by count, so with bodies of up to 4 MB they read megabytes to
return a few fields. Bounding pages by size and reading only core columns for the tree and the
list is its own issue, built before piece 5. (proposed)

- **A library patch** turns that library's request into an outbound call, hands it to the
  interception point, sends the request as the library would have, and reports the response,
  the body as it is read, and any error. It holds no rules. (proposed)
- **An outbound call** is a frozen dataclass: method, URL, headers, and the body if it is in
  memory. Nothing in it belongs to one library. (proposed)
- **The interception point** is one function pair used by every patch. `begin` takes an outbound
  call, records its start and returns a handle for the call. `end` takes the handle and the
  response or the error. Piece 5 adds a decision to what `begin` returns (send a changed request,
  or do not send and use this response), and a patch then also translates in the other direction,
  from an outbound call or a recorded response to its library's own. Pure functions build the
  start and end payloads. (proposed)
- **Provider recognition** is a pure function from method, host and path to a kind and a name.
  (proposed)
- **Credential header replacement** is a pure function from headers to headers with the values
  replaced. (proposed)

Reused, not rebuilt: the error shape (`describe_error`, which moves out of `_labeled_calls.py`
now that it has a second caller), the `$irimi` marker module, the flag words in `irimi._wire`,
the size estimate and the drop count.

### What is recorded for one call

Start event, written before the request is sent:

- `kind`: `llm` when the request matches the provider table, `http` otherwise. (from Notion)
- `name`: for `http`, method, host without the port, and path, as
  `POST api.stripe.com/v1/refunds`. For `llm`, provider and operation, as
  `anthropic messages.create`. (from Notion; leaving the port out is proposed)
- `request`: `method`, `url` (as sent, query string included, without any `user:password@` part,
  which travels as the `Authorization` header), `headers`, `body`. (proposed)

End event, written once, on the first of: the response body is read to its end, the response is
closed, or an exception is raised. (proposed)

- `status`: the call's status. `ok` when the body was read to its end or the app closed the
  response, whatever the HTTP status. `error`, with `error` (`type`, `message`) in the shape a
  labeled function's end uses, when sending or reading raised. (proposed)
- `answered`: `live`, meaning the request was sent. (from Notion; piece 5 adds the values for a
  call answered from a recording or faked)
- `response`: `status` (the HTTP status number), `headers`, `body`. Absent when no response
  arrived. When reading failed part way it holds what was read, flagged
  `response_body_incomplete`. (proposed)

**Headers** are an object from name to value, with names as sent. A value is a string, or the
redaction marker in place of the string. A header sent more than once has its values joined with
`, `. A header named `$irimi` is escaped as piece 1's markers require. (proposed)

**A body** is an object with `encoding`, `content_type` and `data`. `encoding` is `text` when the
recorded bytes are valid UTF-8 and `base64` otherwise, and `content_type` is the value of the
`Content-Type` header. The optional parsed view from piece 1 is not written: it would roughly
double a JSON body on the wire, and a reader can parse `data`. A call with no body has no `body`.
(proposed)

**A compressed response.** A body is recorded in the form the patch sees it, and is never decoded
or re-encoded by the SDK. `content_encoding` on the body names the encoding the recorded bytes
are still in, and is absent when they are not encoded. At the `httpx` and `httpx2` transport that
is the bytes as they arrived, so a gzip response is recorded as gzip, as base64, with
`content_encoding: gzip`, and its recorded headers describe it. A reader decodes it. (proposed;
this keeps piece 1's "a body is recorded as the exact bytes")

**Size.** A body is cut at 4 MB of the bytes being recorded, before they are written as text or
base64, and the call is flagged. A text body is cut at a character boundary. (proposed; Step 1
fixed only that a cut body is flagged and cannot be served in a replay)

The exporter today closes a batch at 100 events. It also closes one when the events in it reach
8 MB by the size the SDK already estimates for the queue limit, and an event larger than that is
sent alone. The reason is memory and the cost of losing a batch: a batch that fails three times
is dropped whole. (proposed)

**Flags** on the event that carries the body (all proposed):

- `request_body_truncated`, `response_body_truncated`: cut at the size limit.
- `request_body_not_recorded`: the request body was a stream, such as a file upload. It is not
  read ahead of the library and is not recorded. Such a call cannot be matched on its body in a
  replay.
- `response_body_incomplete`: the response was closed or failed before all of it was read. What
  was read is recorded.

### Credential headers

The value of each of these, in a request or a response, is replaced by the marker
`{"$irimi": "redacted", "reason": "credential"}` (from Notion, piece 1). Names are matched
case-insensitively. (decided 2026-11-27: the values are left out. The list itself is proposed.)

`Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `Api-Key`,
`Apikey`, `X-Goog-Api-Key`, `X-Auth-Token`, `X-Auth-Key`, `X-Amz-Security-Token`,
`Ocp-Apim-Subscription-Key`, `X-Functions-Key`, `Private-Token`, `X-Vault-Token`,
`X-Shopify-Access-Token`, `DD-API-KEY`.

The list is fixed in code and can be added to in a later release, never shortened (from Notion).
Nothing configures it in this piece, so a credential in a header that is not listed is recorded.
(proposed)

### Where a call sits in a run

- An outbound call is a child of the call that is current when it is made, and takes the next
  `seq` of the run. (from Notion)
- It never becomes the current call. It has no children, and a streamed call can end after the
  code that started it has moved on, so ending it must not change what is current. The recorder
  gains a way to begin and end a call that leaves the current call alone; the decorators keep the
  existing path. (proposed)
- A call made outside any workflow is recorded as its own trace and flagged `unattached`, whether
  or not it is an LLM call, with its `request` (`method`, `url`, `headers`) and its `response`
  (`status`, `headers`) but without bodies. Step 1 left "all calls or only LLM calls" open. All
  of them, because a call on a thread that lost its run is also unattached and Step 1 says it is
  never dropped. Without bodies, because nothing reads them and an app's background traffic would
  otherwise compete with run events for the queue. (proposed)
- `events_produced` on a run's root end is the number of events the SDK produced for the run by
  then, and the store calls a run incomplete when it holds fewer. A stream that ends after its
  workflow returned writes its end event later, so that event is not in the count and its loss
  would not be seen. That is the known limit already recorded for piece 3.
  (decided 2026-11-27, piece 3)
- The SDK's own sending to the store goes through `urllib.request`, which is not patched, so it
  is never recorded. (proposed)

### Recognising an LLM call

One table, matched on host and path. Hosts and paths were read from the clients named under
"Patching" on 2026-11-27.

| Provider | Host | Path | Name |
| --- | --- | --- | --- |
| Anthropic | `api.anthropic.com` | `/v1/messages` | `anthropic messages.create` |
| OpenAI | `api.openai.com` | `/v1/chat/completions` | `openai chat.completions.create` |
| OpenAI | `api.openai.com` | `/v1/responses` | `openai responses.create` |
| OpenRouter | `openrouter.ai` | `/api/v1/chat/completions` | `openrouter chat.completions.create` |
| Gemini | `generativelanguage.googleapis.com` | `/{version}/models/{model}:generateContent` | `gemini models.generate_content` |
| Gemini | `generativelanguage.googleapis.com` | `/{version}/models/{model}:streamGenerateContent` | `gemini models.generate_content` |

A row matches when the host is equal, ignoring case, and the path is equal to the pattern, with
`{version}` and `{model}` each standing for one path segment. Method, scheme, port and query
string are not compared. A request to one of these hosts on any other path, such as
`/v1/messages/count_tokens`, is an `http` call. Both Gemini operations take the one name, because
the model and the streaming choice are in the path. Recognition is a pure function of the
recorded method, host and path, so piece 5 can apply the table of its day to an old recording
instead of trusting the stored `kind`. (proposed)

### Patching

`init` patches each library that is installed and reports the result. (from Notion)

`httpx2` is a fork of `httpx` with the same transport classes (`pydantic/httpx2` on GitHub). Read
2026-11-27: `anthropic` 2.14.0, `openai` 3.60.0 and `google-genai` 3.6.1 require `httpx2` 2.x;
`openrouter` 1.3.212 requires `httpx`; `google-genai` also requires `requests`. The patch was
tried against `httpx` 0.29.0 and `httpx2` 2.16.2.

| Library | Patch target | Covers |
| --- | --- | --- |
| `httpx` | `HTTPTransport.handle_request`, `AsyncHTTPTransport.handle_async_request` | `openrouter`; provider clients from before the move to `httpx2` |
| `httpx2` | the same two names | `anthropic`, `openai`, `google-genai` |
| `urllib3` | candidate: `HTTPConnectionPool.urlopen`, which is called once per attempt | `requests`, and `google-genai` where it uses `requests` |
| `aiohttp` | candidate: `ClientRequest.send`, below the session's redirect loop | async `google-genai` when aiohttp is installed, Anthropic's aiohttp transport |

- `httpx` and `httpx2` have the same two transport classes, so one patch covers both. (proposed)
- `init` imports and patches each of the four libraries that is installed, whether or not the app
  has imported it yet, so a client made later is covered. The cost is that library's import time
  at `init`. A library that is not installed is not reported. (proposed)
- Without a store URL nothing is recorded, so nothing is patched and both lists are empty.
  (proposed)
- A patch target that is missing or already patched is never an error. A missing target puts the
  library in `unpatched` and warns once. (proposed)
- `patched` and `unpatched` hold library names. `unpatched` holds a library `init` saw and did
  not patch, for either reason: its patch target is missing, or its patch is not built yet. Until
  its patch is built, an installed `urllib3` or `aiohttp` is reported in `unpatched`. (proposed)
- `patched` says the library's own transport is patched. It does not say every call through the
  library is recorded: a client given its own transport class, such as a test transport, is not
  covered, and nothing reports it. (proposed)
- The standard library's `http.client` and `urllib.request` are not patched and not listed in
  `unpatched`. A call an app makes through them is not recorded. (proposed)
- Patching replaces a method on the library's class, so clients made before and after `init` are
  both covered. (proposed)
- The response a patch returns is the library's own response with its byte stream wrapped. The
  wrapper is a subclass of the library's own stream class, made when the library is patched,
  because `httpx` refuses any other stream. It yields each chunk unchanged, keeps a copy up to
  the size limit, and ends the call once. A response the app never reads to the end and never
  closes has no end event; its call reads as unfinished and its copy is freed with the response.
  (proposed)

### Failing open

Every step the SDK adds around a request (describing it, recording the start, wrapping the
stream, recording the end) is guarded: a failure is counted through the existing drop count and
the request proceeds as if the SDK were not there. An exception the library itself raises is
recorded and re-raised unchanged. (from Notion)

### Tests

All proposed.

- One test per behaviour, through `irimi.init` and a real client of each library, against a
  local HTTP server on the loopback address, as `packages/sdk/tests/conftest.py` already does for
  the store.
- A request addressed to a provider's host is served by the local server acting as a proxy, so
  the provider is never contacted.
- The main end-to-end test gains an outbound call: recorded by the SDK, kept by the real store,
  read back through the reader API.
- Recognition and credential headers are dense pure logic and get parametrised cases.
- `httpx`, `httpx2`, `requests` and `aiohttp` become development dependencies of the workspace,
  never of the SDK.
- Before each PR is called done, a script that calls a provider's client, pointed at a local
  server so it needs no API key, is run against a local store and the recorded run is read back
  with `irimi runs`.

## Delivery

Four PRs, each with its own issue, in this order. (proposed)

1. **Capture through `httpx` and `httpx2`:** the outbound call, the interception point, bodies,
   sizes, credential headers, streaming, `InitResult`, the wire additions. Every call is `http`.
2. **LLM recognition:** the provider table, `kind` and `name`.
3. **`urllib3`.**
4. **`aiohttp`.**

PRs 3 and 4 each start with a short design note that fixes the patch target and says where that
library departs from the `httpx` behaviour. One known departure: `aiohttp` decodes a compressed
body before client code sees it, so its bodies are recorded decoded, with no `content_encoding`.

## Risks

- **Production data in the store before redaction exists.** Bodies, URLs and unlisted headers
  are stored as sent. Limit: the credential list. Otherwise accepted until hosting.
  (decided 2026-11-27)
- **Work on the app's thread.** Copying up to 4 MB per body and checking it is UTF-8. Limit: the
  size cut; nothing is decoded or parsed.
- **Memory.** Each response being read holds a copy of up to 4 MB, and the queue holds up to
  64 MB.
- **Run events lost to volume.** A full queue drops whole events, a run's own among them. Limit:
  no bodies on unattached calls; drops are counted.
- **A library changes the patched method.** Limit: a missing target puts the library in
  `unpatched`; every added step fails open.
- **Calls not covered,** with nothing reporting it: a client with its own transport, the standard
  library, a streamed request body.
- **A stream that ends after its workflow returned** is not in `events_produced`, so its loss is
  not seen. Known limit from piece 3.
- **Large events make the store's reads slow** until the issue named under "The parts and who
  owns what" is built.

## Needs an OK

Henry gave an OK to all fourteen as proposed on 2026-11-27, with item 7 built as "the URL is
recorded as sent".

1. **Wire additions.** Already in Step 1's worked example and new only to the code: `request`,
   `response`, `answered`, the kinds `http` and `llm`. New to the format: in `request`, `method`,
   `url` and `headers`; in `response`, `headers`; the body object with `encoding`,
   `content_type`, `data` and `content_encoding`; the flags `request_body_truncated`,
   `response_body_truncated`, `request_body_not_recorded` and `response_body_incomplete`. A
   header's value is a string, or the redaction marker in place of the string. The names and flag
   words are added to `irimi._wire`, where the existing ones are defined.
2. **The SDK does not write `gen_ai.request.model` or `usage` in this piece** (option B2). The
   worked example in Step 1 shows both.
3. **The SDK does not write a body's parsed view.**
4. **Every call outside a workflow is recorded, without bodies,** not only LLM calls.
5. **The numbers:** 4 MB per body; a batch closes at 8 MB as well as at 100 events.
6. **The credential header list.**
7. **A Gemini key sent as `?key=` in the URL.** Current `google-genai` sends the key as
   `X-Goog-Api-Key`, which is covered, but a client may put it in the URL. The question: record
   the URL as sent (what "everything else as is" says), or leave out the value of `key` for the
   Gemini host as one exception. Until answered, the URL is recorded as sent.
8. **A compressed body is recorded compressed.** The cost: a gzip response is base64 in the store
   until a reader decodes it.
9. **`status` is `ok` for any HTTP status.**
10. **Repeated headers are joined with `, `,** which cannot be split back for `Set-Cookie`.
11. **Vertex AI, Azure OpenAI and custom base URLs are recorded as `http`,** and a replay treats
    them as `http`.
12. **What `patched` and `unpatched` report** (public SDK surface): library names; every
    installed library is imported and patched at `init`; nothing is patched without a store URL.
13. **Two Step 1 behaviours are left out of this piece:** the flag for a `write=True` function
    that makes an LLM call, and the "contains redacted values" condition.
14. **The store's reads are made cheap for large events in their own issue,** before piece 5.

## Left to the builder

- Module names and the exact dataclasses, within `CLAUDE.md`.
- The oldest version of each library the patch is tested against.
