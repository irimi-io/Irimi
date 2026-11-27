# Piece 3: Read a run

Design for the third of the six pieces of the Step 2 build. Written 2026-11-26.

Each choice below is marked **decided** (Henry said so), **from Notion** (already fixed in Step 1
or Design Decisions) or **proposed** (the author's call, open to correction).

## Intent

Pieces 1 and 2 get a run into the store. Nothing can get one out: the store has one route
(`POST /v1/events`), one read (`StorageEngine.read_run_events`, a whole trace in arrival order) and
the CLI package is empty. Piece 3 builds the read side, so that a recorded run can be listed, read
as a tree, and read back raw for a replay. Pieces 5 (replay) and 6 (`irimi diff`) both stand on it.

## Goals

- A reader can list runs in pages, get one run as a tree, get one call with its bodies, and list
  unattached calls. (from Notion, boundary 3)
- A run can be read back as the events it was written as, in pages. (from Notion, the replay read)
- `irimi runs` lists runs and shows one, and one command starts a local store. (decided: store and
  CLI are both in this piece)
- The CLI is bare: the fewest commands and options that do the above. (decided)
- The storage engine and the way lists are produced can be swapped later without any reader
  changing. (decided)

## Non-goals

- Keys. Nothing checks a read key or a write key yet. A localhost store needs none.
- The SDK side of the replay read. The SDK calls it in piece 5; this piece builds the store side.
- The condition "contains redacted values". Nothing is redacted until piece 4, and the function
  that finds markers is scheduled with replay. It is added with redaction. (proposed)
- Diffs, pairing, filters on the run list, deleting runs, retention.
- A second storage engine.

## Anti-goals

- A reader that has to know how the store keeps its data. The CLI never opens the database.
- A second copy of any document shape. Each shape is defined once and imported.
- A tree that hides a gap. A missing end, a missing start or a missing parent is shown, not
  tidied away. (from Notion)

## Constraints

- CLAUDE.md in full: no comments, typed throughout under `mypy --strict`, logic apart from I/O,
  frozen dataclasses across module boundaries, tests through public interfaces only, no network in
  tests, dependencies need a reason.
- Events arrive in any order, more than once, and a run can keep growing after it is first read.
- Unknown fields are kept and returned. Unknown event types are kept. Unknown kinds, flags and
  conditions must not break a reader.
- Boundary 2 and boundary 3 each carry their own version number.
- Until keys exist every event belongs to the single project `default`.

## Approaches

The fork is where the run list comes from. A list needs each run's name, start time, status and
condition without loading every event of every run.

1. **Query the events table.** A run is a root start event. Its end event and its count of
   received events are looked up beside it. Fields the list filters on get their own column and an
   index. Fits the decided rule "a column for each field the main pipeline uses". Nothing has to be
   kept in sync, so late and repeated events need no extra handling. A list page does a little more
   work per row.
2. **A runs table updated at ingest.** Cheapest lists and easy filters. Every storage engine has to
   implement the same update logic for out-of-order and repeated events, and the summary can drift
   from the events it is derived from.
3. **Assemble every run to list it.** Reads every event of every run on a page. Dropped.

**Picked: 1** (decided 2026-11-26), "for now", on the condition that 2 stays reachable. It does:
the list is one method on `StorageEngine` that returns plain data, so a runs table is a change
inside one engine.

## Design

### The parts and who owns what

```
SDK ──POST /v1/events──────────────► ingest ─► StorageEngine ◄─ reads ─┬─ replay read  GET /v1/runs/{id}/events
                                                                       └─ assembly ─► reader API  GET /api/v1/...
                                                                                          ▲
                                                                     irimi runs ──HTTP────┘
```

- **`StorageEngine`** (store): keeps events and answers four reads with plain data. It knows
  nothing about trees, conditions or JSON documents.
- **Assembly** (store): pure functions from events to a tree and to a run's conditions. No I/O.
- **Reader documents** (store): the boundary 3 shapes, defined once.
- **Routes** (store): HTTP in, a storage read, assembly, a document out.
- **CLI**: HTTP to the store and printing. It imports the reader document shapes from the store
  package and never touches storage.

### Storage reads

`StorageEngine` keeps `add_events` and gains reads. Every paged read takes an opaque cursor and a
limit and returns a page with the next cursor, or none at the end. The cursor's meaning belongs to
the engine, so an engine can change how it pages. A cursor an engine cannot read raises a specific
error, answered with 400.

- **Read a run's events, in pages,** in arrival order. Replaces today's `read_run_events`.
- **Read one call's events:** every event of one span in one run.
- **List roots, in pages:** for each root start event, the start, its end event if one arrived, and
  how many events the store holds for that trace. Takes whether to list runs or unattached calls.
  Newest first by the start's `time_unix_nano`, ties broken by arrival.

A root is a start event with no parent. It is an unattached call when its flags include
`unattached`, and a run otherwise. Kind is not used to tell them apart, because a later writer can
start a run with a kind other than `workflow`. (proposed)

In SQLite: one new column for "this event is flagged unattached", and indexes for the root list
and for finding a span's events. `events_produced` needs no column, because it is read from the
root's end event, which the list already loads. (proposed)

The table already exists in files made by piece 2, so the schema gets a version (`PRAGMA
user_version`) and each change is a numbered step that runs once on open. The first step adds the
column, fills it for existing rows and creates the indexes. (proposed)

**Known limit:** a run whose root start never arrived is not in the list. It can still be read by
id. Listing it would need a scan of every event or the runs table of approach 2. (proposed)

### Assembly

Pure functions over a run's events.

- **Join.** A node is one span: its start, its end if any, and any events of other types on it.
- **Tree.** A node hangs under the node its `parent_span_id` names. Children are in `seq` order.
- **Gaps are kept.**
  - A node with no end has no end time and no status.
  - A node with an end but no start has no kind, name, parent or `seq`.
  - A node whose parent is not in the run, and any root after the first, goes in a `detached` list
    beside the tree, with its own children under it.
- **Fields.** A node has the common core (span id, parent, kind, name, `seq`, start and end time,
  status, flags) and every other field of its start and end events, flat, as decided for the one
  node shape. Flags are the union of both events' flags. If the same other field is on both, the
  end's value is the one shown; the raw read still has both. (proposed)
- **Other event types.** Events that are neither start nor end are listed on their node as written.
  (proposed)
- **Conditions.** A list of words, empty when nothing is wrong, so a new condition is an addition
  and a reader can show one it does not know. (proposed: a list of words, not one field each)
  - `unfinished`: the root has no end.
  - `incomplete`: the store holds fewer events for the trace, of any type, than the root's end
    says were produced. (from Notion) It is not known for a run with no root end.
  - `not_replayable`: the root's flags say so.

Conditions are worked out from the root's start, the root's end and the count of events held, and
nothing else. The list has exactly those three, so one function serves the list and the tree and
they agree for every run with one root. A trace with more than one root is the exception, below. A structural gap (a missing start, a missing parent, no root) is not a
condition word: the tree shows it directly, as above. (proposed)

A trace with more than one root start is a writer's mistake. The tree's `root` is the one with the
lowest `seq`, then the earliest arrival, and the others are in `detached`. The list has a row per
root span and does not merge them, so its rows for the other roots can state conditions the tree
does not. A span started twice is one row, made from the start that arrived first. (proposed)

### Bodies

From Notion: the tree comes without request and response bodies, and a reader fetches them per
call. In the tree, `request.body` and `response.body` keep their small fields (`encoding`,
`content_type`) and lose `data` and `json`. The call-by-id read returns the node whole. The names
`request`, `response`, `body`, `data` and `json` are added to the wire module's field names, where
optional field names are defined once. (proposed)

Nothing records an HTTP or LLM call until piece 4, so this is tested with the worked-example run
from Step 1, which the SDK tests already hold as a fixture. A function's `input` and `output` stay
in the tree.

### The reader API (boundary 3)

Under `/api/v1/`, apart from boundary 2's `/v1/`, so the two versions move separately. (proposed;
shown to Henry, not yet confirmed)

| Route | Answers |
| --- | --- |
| `GET /api/v1/runs?limit=&cursor=` | `{"runs": [...], "next_cursor": ...}` |
| `GET /api/v1/runs/{trace_id}` | `{"run": {...}}` |
| `GET /api/v1/runs/{trace_id}/calls/{span_id}` | `{"call": {...}}` |
| `GET /api/v1/unattached-calls?limit=&cursor=` | `{"calls": [...], "next_cursor": ...}` |

- A run in the list: `id` (the trace id), the root's core fields, `environment`, `mode` and
  `conditions`. No input or output.
- A run as a tree: `id`, `conditions`, `root` (or null) and `detached`. It is not paged.
- A call: the node with its bodies and without its children.
- An unattached call in the list has the shape of a run in the list. Its tree is read with the run
  route, since an unattached call is its own trace.
- Times are strings of nanoseconds, as on the wire.
- A node's keys (proposed; Notion leaves field names to this build): `span_id`,
  `parent_span_id`, `kind`, `name`, `seq`, `start_time_unix_nano`, `end_time_unix_nano`, `status`,
  `flags`, `children`, `events` (the other-type events), then every other field flat. A run adds
  `id` and `conditions`.
- A core value the store does not have is `null`, never an absent key.
- If an event carries an unknown field with the name of one of the node's own keys, the node's
  key wins. The raw read still has the field. (proposed)
- An id the store holds nothing for is a 404 with `{"error": ...}`. An unknown version under
  `/api/` is a 404 naming the accepted versions, as `/v1/` does today.
- The document shapes follow the wire module's pattern: frozen dataclasses with a function to
  write each and a function to parse each, unknown fields kept.

### The replay read (boundary 2)

`GET /v1/runs/{trace_id}/events?limit=&cursor=` answers `{"events": [...], "next_cursor": ...}`,
with each event exactly as it was written, in arrival order. It is the batch shape the SDK already
writes, so the existing batch parser reads it. An id the store holds nothing for is a 404. The path
is defined in the wire module next to the events path. (proposed; an addition to boundary 2, so it
needs Henry's OK)

### The CLI

One console script, `irimi`, in `irimi-cli`. Built on `argparse` and `urllib`: no new dependency.
`irimi-cli` depends on `irimi-store`, for the reader document shapes and to start a store.

| Command | Does |
| --- | --- |
| `irimi runs` | Prints one line per run: id, name, start, duration, status, conditions. |
| `irimi runs <id>` | Prints the run's tree, one indented line per call. |
| `irimi store` | Starts a local store and prints its address. |

- `--json` on `irimi runs` prints the store's answer byte for byte. (from Notion)
- `--limit` and `--cursor` on the list. When there is a next page, the last line gives the cursor.
- The store's address comes from `--store-url`, else `IRIMI_STORE_URL`. With neither, the command
  fails and says so. There is no default address, as decided for `init`.
- `irimi store` takes `--port` and `--database`. It calls one function in `irimi-store` that
  builds the engine and serves the app, so the CLI never imports storage. (proposed: the name
  `irimi store`, and defaults of port 7474 and `irimi-store.db` in the current directory)
- A failure (store unreachable, no such run) prints one line to stderr and exits non-zero.
- Unattached calls and call-by-id are in the API only. (proposed)

### Tests

- **End to end,** in the CLI package: start a store with `irimi store`, run an app that records a
  run, then read it with `irimi runs --json` and `irimi runs <id>`.
- **Store, through HTTP:** events posted, then read. Cases by parametrize: out of order, repeated,
  unfinished, incomplete, not replayable, an end with no start, a missing parent, unknown fields and
  event types kept, bodies left out of the tree and present on the call, pages that stay correct
  while events arrive, unattached calls listed apart from runs.
- **A database file made by piece 2** opens and lists its runs.
- No test reaches into assembly or storage privately.

## Delivery

Two issues and two PRs (proposed): the store's reads and routes first, then the CLI with the
end-to-end test.

## Left to the builder

- Module and function names, and how the store package is split into modules.
- Page size defaults and limits.
- The exact SQL, index definitions and cursor encoding.
- The exact columns and wording of the CLI's printed output.
- Whether the in-memory engine used in today's tests is kept or the tests move to SQLite.
