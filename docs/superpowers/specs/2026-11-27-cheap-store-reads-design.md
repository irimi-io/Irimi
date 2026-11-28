# Keep the store's reads cheap when events hold bodies

Design note for issue #14. Written 2026-11-27. It follows item 14 of "Needs an OK" in
`docs/superpowers/specs/2026-11-27-capture-http-and-llm-calls-design.md` and the reads designed in
`docs/superpowers/specs/2026-11-26-read-a-run-design.md`.

Each choice is marked **from Notion** (already on the Design Decisions page) or **proposed** (the
author's call; Henry was away when this was written). Nothing here changes the wire format or a
reader document. One proposal touches the wording of a recorded decision and is listed under
"To confirm".

## What is slow today

Since piece 4 an event can hold a request body and a response body of up to 4 MB each. The store
keeps every field that has no column of its own in one `data` column of the `events` table, and
three reads load that column for every event they touch:

- **The tree read** loads every event of the run whole, parses it, and then drops everything but
  the core fields.
- **The run list** loads each listed root's start and end whole, so a workflow's input and output
  are read to print one line.
- **The replay read** pages by count, 500 events by default, so one answer can hold a whole run.

Measured against a real `irimi store` on this laptop (SQLite 3.53, Python 3.12, file cache warm,
median of five reads, a fresh store process for each line). The database is 1.07 GB: 20 runs of
ten LLM calls with a 2 MB request and a 1 MB response, one run of 40 calls with 4 MB each way, and
50 runs with no calls whose root has 1 MB of input and 1 MB of output. "Store memory" is how far
the store's resident memory rose above its idle level, 35 MB, during the reads.

| Read | Time | Answer | Store memory |
| --- | --- | --- | --- |
| Run list, 20 runs with small roots | 1.1 ms | 5 KB | +2 MB |
| Run list, 50 runs with 1 MB in and out | 62.9 ms | 12 KB | +158 MB |
| Tree, 22 events holding 30 MB | 19.1 ms | 3 KB | +63 MB |
| Tree, 82 events holding 320 MB | 201.4 ms | 9 KB | +643 MB |
| Call, one call holding 3 MB | 6.9 ms | 3 MB | +14 MB |
| Replay read, 30 MB run, first page | 55.5 ms | 30 MB, the whole run | +249 MB |
| Replay read, 320 MB run, first page | 594.4 ms | 320 MB, the whole run | +1,323 MB |

The time is small on a warm cache. The cost that matters is that time and memory grow with the
bytes held, not with what is returned: a 9 KB tree costs 643 MB of memory, and one replay request
costs four times the run's size. With a cold cache the same bytes are read from disk.

**The existing schema does not let these reads avoid `data`.** The tree and the list need
`status`, `flags`, `environment`, `mode` and `events_produced`, and those are only inside `data`.
Giving them columns in the same table is not enough either. SQLite keeps a large row as a chain of
overflow pages, and a column stored after `data` is reached by walking the chain. Tried on the
320 MB run, reading 82 rows directly in SQLite:

| How the core fields are read | Time |
| --- | --- |
| Today: `data` loaded and parsed in Python | 190 ms |
| No schema change: `json_extract` over `data` in SQL | 56 ms |
| New columns added to `events`, which SQLite stores after `data` | 20 ms |
| The same columns with a covering index | 0.28 ms |
| `data` in a table of its own | 0.28 ms |

## The change

All proposed.

1. **`data` moves to a table of its own.** `events` keeps one small row per event. `event_data`
   holds the catch-all column, one row per event, keyed by the event's `arrival`. Any read of
   `events` is then cheap whatever index it uses, and a later column can be added and filled
   without rewriting any body.
2. **Five fields get their own columns** in `events`: `status`, `flags`, `environment`, `mode` and
   `events_produced`. Each holds the field's JSON text, and is empty when the event does not carry
   the field, so a value of any type and the difference between absent and `null` are kept. These
   fields are no longer written into `data`. `unattached` stays as it is.
3. **`events` gains `data_size`,** the length of the event's `data`.
4. **The tree and the list read `events` only.** `StorageEngine` gains a read that returns a run's
   events with only the fields that have a column, and `list_roots` returns roots the same way.
   Assembly is unchanged: it already uses only those fields for a tree node, a list row and the
   conditions.
5. **A replay page is bounded by size as well as by count.** A page ends before the event that
   would take its `data_size` total over 8 MB, the size at which the SDK closes a batch. An event
   larger than that travels alone. The page is chosen from `arrival` and `data_size` by a pure
   function before any body is read, so no body is read that is not returned.
6. **The call read** is unchanged in behaviour. It joins the two tables for one call's events.

Tried in SQLite on the same data in the new layout: the core rows of the 320 MB run read in
0.28 ms, the list's root rows with their ends in 0.46 ms, and one 8 MB replay page in 2.5 ms.

**Not picked:** new columns in `events` with covering indexes. It reads as fast, but it is cheap
only while SQLite picks the covering index for every query, nothing reachable by a test shows when
it stops, and filling any later column rewrites every stored body again.

### Carrying an existing database forward

A database is at schema version 2 today. Step 3 runs once when the store opens it, in one
transaction, as steps 1 and 2 do (from Notion: numbered steps, a newer schema is refused):

1. Rename `events`, create the new `events` and `event_data`.
2. Copy each event across, one at a time: the five fields go to their columns, the rest of `data`
   to `event_data`. `arrival` is kept, so a cursor issued before the upgrade still works.
3. Drop the renamed table and create the four indexes again.

Tried on a 987 MB database of 562 events: 3.4 s. The write-ahead log grew to 948 MB during the
step, and the file ended at 1.88 GB with half of it free pages that later events reuse. The step is
one-way: an older store refuses the file.

## What stays the same at the boundaries

- Every route, every document shape and every refusal. The wire format is untouched.
- An event is read back as it was written, in arrival order. A cursor is still opaque, and one
  issued before the change still works.
- `limit` is read and refused as before, and still caps a page's count.
- The one visible change: a replay page can hold fewer events than `limit` and still have a
  `next_cursor`. Page sizes are not part of the contract (from Notion). A reader that follows
  `next_cursor` until it is null needs no change. The CLI does not use the replay read.
- The tree is still not paged and the list is still one row per run (from Notion).

## Tests

Through the store's HTTP API, as the existing ones are.

- A run of large events is read in more than one page, no page with more than one event is over
  8 MB, and the pages together are the run as written.
- An event carrying any of the five fields with a value of any type, `null`, or not at all is read
  back as written.
- A database file made at schema version 2, with bodies, opens and answers the replay read, the
  list, the unattached list, the tree and the call read as before, and takes new events.
- The existing tests of the list, the tree, conditions and older files pass unchanged.
- No test asserts a time. The measurement above is repeated before and after against a real local
  store, on a copy of the same database, and `irimi runs` is run against it.

## Risks

- **The first start after the upgrade is slow and needs disk.** About 3.4 s per GB here, with free
  space of about twice the database's size while it runs. A second store started on the same file
  during the step waits 30 seconds and then refuses to start.
- **The file does not shrink.** After the step about half of it is free pages. They are reused, not
  returned to the disk.
- **A mistake in step 3 loses data.** Limit: one transaction, the old table dropped last, and the
  test that reads an older file back as written.
- **An ingest writes two rows per event.** Not measured as a cost; the measurement task reports the
  time to fill the database before and after.
- **A replay of a run with large bodies takes many requests.** The 320 MB run is 82 pages. The
  8 MB bound is one constant.
- **A writer that puts something large in one of the five fields** makes the core rows large
  again. No known writer does.
- **The call read and one replay page** still hold up to one call's bodies in memory at once.

## To confirm

Nothing needs an OK under the boundary rules: no wire format change, no reader document change.

One item for Henry to confirm, because it touches the wording of a recorded decision. Design
Decisions says a stored event has "its own column for each field the main pipeline uses and one
catch-all data column for everything else". This design keeps both, and puts the catch-all column
in a second table of the same SQLite engine. (proposed)

## Left to the builder

- Module, function and table names, within `CLAUDE.md`.
- The exact SQL.
