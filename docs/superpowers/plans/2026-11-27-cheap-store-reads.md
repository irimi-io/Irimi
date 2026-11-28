# Cheap Store Reads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The tree read and the run list cost the same whatever bodies a run's events hold, and one replay page holds at most 8 MB of event data.

**Architecture:** The SQLite engine keeps each event as a small row in `events` and its catch-all `data` in a second table, `event_data`. Five fields the tree and the list use (`status`, `flags`, `environment`, `mode`, `events_produced`) get their own columns. The tree and the list read `events` only. The replay read picks its page from `arrival` and `data_size` with a pure function, then reads only the bodies it returns. A schema step carries an existing database forward.

**Tech Stack:** Python 3.12, the standard library's `sqlite3`, Starlette. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-27-cheap-store-reads-design.md`. Read it and `CLAUDE.md` before any task.

**Scope:** One PR on the branch `14-cheap-store-reads`, which exists and holds the spec and this plan. The PR body says `Closes #14`.

**Before starting:** the spec has one item under "To confirm": `data` in a table of its own, against the wording "one catch-all data column" on the Design Decisions page. This plan builds it as proposed. If Henry has answered otherwise, stop and say so; Tasks 2 and 3 change.

## Global Constraints

- `CLAUDE.md` in full. In particular: no comments and no docstrings in the store; full names; frozen dataclasses across module boundaries; `mypy --strict`; no `utils` or `helpers`; tests only through the store's HTTP API; no network beyond the loopback address.
- No route, document shape, refusal or wire shape changes. The only change a reader can see is how many events a replay page holds. If a task seems to need more, stop and report it.
- Only the store package changes: `packages/store/src/irimi_store` and `packages/store/tests`. The SDK and the CLI are not touched.
- A schema step is a record of what was done. Steps 1 and 2 in `sqlite_schema.py` are not edited, and step 3 spells out its own SQL and its own five field names, so a later change to the engine's column list cannot change what step 3 does.
- No `# type: ignore`.
- Before every commit all of these pass: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run mypy --python-version 3.10 packages/sdk/src`, `uv run pytest`.
- Exact values from the spec: the page size bound is `8 * 1024 * 1024`; the new schema version is 3; the five fields are `status`, `flags`, `environment`, `mode`, `events_produced`.
- Commit messages: imperative, what changed and why, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## Review Focus

1. **A value that is not what the SDK writes.** `status: true`, `flags: "odd"`, `events_produced: "2"`, `mode: {"a": [1]}`, any of the five as `null`, any of them absent. Each is read back exactly as written, and `null` is not confused with absent. (Task 2)
2. **An event sent twice.** `INSERT OR IGNORE` skips the second copy in `events`; nothing is written to `event_data` for it and nothing raises. (Task 2, covered by `test_events_are_kept_as_written_whatever_order_or_repeats_they_arrive_in`)
3. **An older database.** A file at schema version 0 and a file at version 2 both open, keep every event, keep their `arrival` numbers, and take new events after the old ones. Six processes opening one older file at once still all succeed. (Task 2)
4. **An event larger than the page bound.** It is returned alone, and the read moves on to the next page. A page is never empty while events remain. (Task 4)
5. **No body read that is not returned.** After Task 3 no SQL statement used by the tree or the list names `event_data`, and after Task 4 the replay read's first statement does not either.

## File Structure

```
packages/store/src/irimi_store/
  storage.py            modify: CORE_FIELD_NAMES, read_run_core_events, size_limit on read_run_events
  stored_fields.py      create: StoredFields, split_fields, join_fields
  sqlite_schema.py      modify: step 3
  sqlite_storage.py     modify: two tables on write, core reads, the sized replay page
  pages.py              modify: count_items_that_fit
  reader_routes.py      modify: the tree reads core events; the paging loop goes
  app.py                modify: EVENT_PAGE_SIZE_LIMIT
packages/store/tests/
  test_replay_read.py   modify: fields with their own column, pages bounded by size
  test_older_store_files.py   create: a file at schema version 2 is carried forward
```

`assembly.py`, `reader_documents.py`, `page_requests.py`, `flags.py`, `answers.py` and `server.py` do not change.

---

### Task 1: Measure the reads as they are

Nothing in the repo measures a read, so this task makes a throwaway script. It lives outside the repo and is never committed. Task 5 runs it again.

- [ ] **Step 1: Write the script**

`mkdir -p "$TMPDIR/irimi-14"` and save this as `$TMPDIR/irimi-14/measure_reads.py`:

```python
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager

MEGABYTE = 1024 * 1024
SENTENCE = "The customer was charged twice for one order. "
CLOCK = 1795341600000000000


def text(size):
    return (SENTENCE * (size // len(SENTENCE) + 1))[:size]


def body(size):
    return {"encoding": "text", "content_type": "application/json", "data": text(size)}


def run_events(trace, calls, request_size, response_size, root_size):
    yield {
        "id": f"{trace}-root-start",
        "type": "start",
        "trace_id": trace,
        "span_id": "root",
        "time_unix_nano": str(CLOCK),
        "parent_span_id": None,
        "kind": "workflow",
        "name": "handle_ticket",
        "seq": 0,
        "mode": "normal",
        "environment": "production",
        "input": {"args": [text(root_size)], "kwargs": {}},
    }
    for number in range(1, calls + 1):
        yield {
            "id": f"{trace}-c{number}-start",
            "type": "start",
            "trace_id": trace,
            "span_id": f"c{number}",
            "time_unix_nano": str(CLOCK + number),
            "parent_span_id": "root",
            "kind": "llm",
            "name": "anthropic messages.create",
            "seq": number,
            "request": {
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages",
                "headers": {"content-type": "application/json"},
                "body": body(request_size),
            },
        }
        yield {
            "id": f"{trace}-c{number}-end",
            "type": "end",
            "trace_id": trace,
            "span_id": f"c{number}",
            "time_unix_nano": str(CLOCK + number + 1),
            "status": "ok",
            "answered": "live",
            "response": {
                "status": 200,
                "headers": {"content-type": "application/json"},
                "body": body(response_size),
            },
        }
    yield {
        "id": f"{trace}-root-end",
        "type": "end",
        "trace_id": trace,
        "span_id": "root",
        "time_unix_nano": str(CLOCK + 10_000),
        "status": "ok",
        "output": text(root_size),
        "events_produced": 2 * calls + 2,
    }


RUNS = [
    *((f"report-{number}", 0, 0, 0, MEGABYTE) for number in range(50)),
    ("long-run", 40, 4 * MEGABYTE, 4 * MEGABYTE, 200),
    *((f"ticket-{number}", 10, 2 * MEGABYTE, MEGABYTE, 200) for number in range(20)),
]


@contextmanager
def local_store(irimi, database):
    began = time.perf_counter()
    store = subprocess.Popen(
        [irimi, "store", "--port", "0", "--database", database], stdout=subprocess.PIPE, text=True
    )
    try:
        store_url = store.stdout.readline().strip()
        yield store, store_url, time.perf_counter() - began
    finally:
        store.terminate()
        store.wait(timeout=30)
        store.stdout.close()


def fill(irimi, database):
    began = time.perf_counter()
    with local_store(irimi, database) as (_, store_url, _):
        for run in RUNS:
            for event in run_events(*run):
                request = urllib.request.Request(
                    store_url + "/v1/events", data=json.dumps({"events": [event]}).encode()
                )
                with urllib.request.urlopen(request, timeout=120) as answer:
                    assert json.loads(answer.read()) == {"refused": []}
    print(f"filled in {time.perf_counter() - began:.2f} s")


def resident_megabytes(pid):
    shown = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(shown.stdout or 0) / 1024


def read(store_url, path):
    began = time.perf_counter()
    with urllib.request.urlopen(store_url + path, timeout=600) as answer:
        document = answer.read()
    return time.perf_counter() - began, document


def read_every_page(store_url, path):
    seconds, sizes, cursor = 0.0, [], None
    while True:
        spent, document = read(store_url, path + (f"?cursor={cursor}" if cursor else ""))
        seconds += spent
        sizes.append(len(document))
        cursor = json.loads(document)["next_cursor"]
        if cursor is None:
            return seconds, sizes


def measure(irimi, database, name, take_reading, repeats=5):
    with local_store(irimi, database) as (store, store_url, _):
        before = resident_megabytes(store.pid)
        peak = [before]
        is_done = threading.Event()

        def watch():
            while not is_done.wait(0.005):
                peak.append(resident_megabytes(store.pid))

        watcher = threading.Thread(target=watch)
        watcher.start()
        readings = [take_reading(store_url) for _ in range(repeats)]
        is_done.set()
        watcher.join()
    milliseconds = statistics.median(seconds for seconds, _ in readings) * 1000
    sizes = readings[0][1]
    print(
        f"{name:<46} {milliseconds:>8.1f} ms  {len(sizes):>3} answer(s), largest"
        f" {max(sizes) / MEGABYTE:>7.2f} MB  store memory +{max(peak) - before:>5.0f} MB"
    )


def one(path):
    def take_reading(store_url):
        seconds, document = read(store_url, path)
        return seconds, [len(document)]

    return take_reading


def measure_all(irimi, database):
    with local_store(irimi, database) as (_, store_url, seconds_to_start):
        print(f"store ready {seconds_to_start:.2f} s after it was started")
        _, document = read(store_url, "/api/v1/runs?limit=21")
        cursor = json.loads(document)["next_cursor"]
    readings = [
        ("run list, 20 runs with small roots", one("/api/v1/runs?limit=20")),
        ("run list, 50 runs with 1 MB in and out", one(f"/api/v1/runs?cursor={cursor}")),
        ("tree, ticket-0 (22 events, 30 MB)", one("/api/v1/runs/ticket-0")),
        ("tree, long-run (82 events, 320 MB)", one("/api/v1/runs/long-run")),
        ("call, ticket-0 c1 (3 MB)", one("/api/v1/runs/ticket-0/calls/c1")),
        ("replay read, ticket-0, first page", one("/v1/runs/ticket-0/events")),
        ("replay read, long-run, first page", one("/v1/runs/long-run/events")),
        (
            "replay read, long-run, every page",
            lambda store_url: read_every_page(store_url, "/v1/runs/long-run/events"),
        ),
    ]
    for name, take_reading in readings:
        measure(irimi, database, name, take_reading)


if __name__ == "__main__":
    command, irimi, database = sys.argv[1:]
    {"fill": fill, "measure": measure_all}[command](irimi, database)
```

It starts a real `irimi store` for the fill and a fresh one for each line it prints, reads through HTTP, and samples the store process's resident memory while it reads. It needs about 1.1 GB of disk for the database.

- [ ] **Step 2: Fill a database and measure, before any code change**

From the worktree root, with `git status` clean:

```
uv sync
.venv/bin/python "$TMPDIR/irimi-14/measure_reads.py" fill .venv/bin/irimi "$TMPDIR/irimi-14/before.db"
.venv/bin/python "$TMPDIR/irimi-14/measure_reads.py" measure .venv/bin/irimi "$TMPDIR/irimi-14/before.db"
```

Expected, within the spread of another machine (these are the spec's numbers):

```
run list, 20 runs with small roots                  1.1 ms    1 answer(s), largest    0.00 MB  store memory +    2 MB
run list, 50 runs with 1 MB in and out             62.9 ms    1 answer(s), largest    0.01 MB  store memory +  158 MB
tree, ticket-0 (22 events, 30 MB)                  19.1 ms    1 answer(s), largest    0.00 MB  store memory +   63 MB
tree, long-run (82 events, 320 MB)                201.4 ms    1 answer(s), largest    0.01 MB  store memory +  643 MB
call, ticket-0 c1 (3 MB)                            6.9 ms    1 answer(s), largest    3.00 MB  store memory +   14 MB
replay read, ticket-0, first page                  55.5 ms    1 answer(s), largest   30.01 MB  store memory +  249 MB
replay read, long-run, first page                 594.4 ms    1 answer(s), largest  320.03 MB  store memory + 1323 MB
replay read, long-run, every page                 591.1 ms    1 answer(s), largest  320.03 MB  store memory + 1281 MB
```

- [ ] **Step 3: Keep the output and the database**

Save the two commands' output to `$TMPDIR/irimi-14/before.txt`. Keep `before.db` untouched: it is a real database at schema version 2 with bodies, and Task 5 carries a copy of it forward. Check with `sqlite3 "$TMPDIR/irimi-14/before.db" "PRAGMA user_version"`, which prints `2`.

Nothing is committed in this task.

### Task 2: Keep `data` in its own table and give five fields columns

Every read answers as before. This task changes where the engine keeps things and carries an older database forward.

**Files:**
- Create: `packages/store/src/irimi_store/stored_fields.py`
- Modify: `packages/store/src/irimi_store/storage.py`, `sqlite_schema.py`, `sqlite_storage.py`
- Test: `packages/store/tests/test_replay_read.py`, `packages/store/tests/test_older_store_files.py`

**Interfaces produced:**

```python
# irimi_store.storage
CORE_FIELD_NAMES: tuple[str, ...] = (STATUS, FLAGS, ENVIRONMENT, MODE, EVENTS_PRODUCED)


# irimi_store.stored_fields
@dataclass(frozen=True)
class StoredFields:
    core: tuple[str | None, ...]
    data: str


def split_fields(
    fields: Mapping[str, JsonValue], core_field_names: Sequence[str]
) -> StoredFields: ...
def join_fields(stored: StoredFields, core_field_names: Sequence[str]) -> dict[str, JsonValue]: ...
```

The schema after step 3 (`PRAGMA user_version` is 3):

```sql
CREATE TABLE events (
    arrival INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    type TEXT NOT NULL,
    span_id TEXT NOT NULL,
    time_unix_nano INTEGER NOT NULL,
    parent_span_id TEXT,
    kind TEXT,
    name TEXT,
    seq INTEGER,
    status TEXT,
    flags TEXT,
    environment TEXT,
    mode TEXT,
    events_produced TEXT,
    data_size INTEGER NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    unattached INTEGER NOT NULL,
    UNIQUE (project, trace_id, id)
);
CREATE TABLE event_data (
    arrival INTEGER PRIMARY KEY,
    data TEXT NOT NULL
);
```

with the four indexes step 2 made (`events_roots`, `events_run_roots`, `events_spans`, `events_runs`), unchanged.

Decisions the signatures leave open:

- `split_fields` returns, in the order of `core_field_names`, `json.dumps(fields[name])` for a field that is present and `None` for one that is not, and `data` as `json.dumps` of the remaining fields. So a present `null` is the text `null` and an absent field is SQL `NULL`.
- `join_fields` is its inverse: `json.loads(data)` plus `json.loads` of each core text that is not `None`.
- `data_size` is `len(stored.data)`. `json.dumps` writes ASCII, so that is its size in bytes.
- `add_events` can no longer use `executemany`. For each event it runs the `INSERT OR IGNORE` into `events`, and only when `cursor.rowcount == 1` inserts `(cursor.lastrowid, stored.data)` into `event_data`. All of a batch stays in the one transaction `_open_connection` gives.
- A whole event is read with `FROM events JOIN event_data USING (arrival)`. `_read_event` builds its document from the columns and `join_fields`. In this task `read_run_events`, `read_call_events`, `list_roots` and `_read_root` all still read whole events; Task 3 narrows the last two.
- Step 3 in `sqlite_schema.py`, in this order inside the transaction `_run_next_step` already opens:
  1. `ALTER TABLE events RENAME TO events_holding_data`. Its indexes go with it and keep their names.
  2. Create `events` and `event_data` as above.
  3. Copy one event at a time: `SELECT ... FROM events_holding_data WHERE arrival > ? ORDER BY arrival LIMIT 1`, starting from 0, until no row comes back. For each, `json.loads` its `data`, `split_fields` with step 3's own tuple of the five names, insert into `events` with the same `arrival`, `created_at_unix_nano` and `unattached`, and insert the rest into `event_data`. One row at a time, so memory holds one event.
  4. `DROP TABLE events_holding_data`, which drops its indexes.
  5. Run step 2's four `CREATE INDEX` statements again, by their existing constants.
- Inserting an explicit `arrival` into an `AUTOINCREMENT` table moves its sequence on, so the next new event arrives after every copied one. Nothing has to set the sequence.
- This was tried on a 987 MB database: 3.4 s, and the reads in the new layout use `events_runs`, `events_spans` and `events_roots` as before.

- [ ] **Step 1: Write the guard for fields that get a column**

In `packages/store/tests/test_replay_read.py`:

```python
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"status": None, "flags": None, "environment": None, "mode": None, "events_produced": None},
        {
            "status": True,
            "flags": "odd",
            "environment": 7,
            "mode": {"a": [1]},
            "events_produced": "2",
        },
        {"status": "ok", "flags": ["unattached", {"later": 1}], "events_produced": 2.5},
    ],
)
def test_a_field_the_store_keeps_apart_is_read_back_as_written(
    store: Any, fields: dict[str, Any]
) -> None:
    events = [{**RUN_START, **fields}, {**RUN_NOTE, **fields}, {**RUN_START, "id": "e9"}]
    store.post("/v1/events", {"events": events})

    assert store.get("/v1/runs/t7c1/events")[1]["events"] == events
```

- [ ] **Step 2: Write the guard for an older database**

Create `packages/store/tests/test_older_store_files.py`:

```python
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from irimi_store.sqlite_storage import SqliteStorageEngine

RECORDED_RUN = json.loads(
    (Path(__file__).parents[2] / "sdk/tests/fixtures/recorded_run.json").read_text()
)["events"]
LOOSE_CALL = {
    "id": "u1",
    "type": "start",
    "trace_id": "t9",
    "span_id": "s1",
    "time_unix_nano": "1795341700000000000",
    "parent_span_id": None,
    "kind": "http",
    "name": "GET example.com/",
    "seq": 0,
    "flags": ["unattached"],
    "status": None,
}
LATE_NOTE = {
    "id": "e9",
    "type": "added_by_a_newer_sdk",
    "trace_id": "t7c1",
    "span_id": "s1",
    "time_unix_nano": "1795341603000000000",
    "status": True,
}
COLUMNS_AT_SCHEMA_VERSION_2 = (
    "trace_id",
    "id",
    "type",
    "span_id",
    "time_unix_nano",
    "parent_span_id",
    "kind",
    "name",
    "seq",
)
STORE_FILE_AT_SCHEMA_VERSION_2 = """
CREATE TABLE events (
    arrival INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    type TEXT NOT NULL,
    span_id TEXT NOT NULL,
    time_unix_nano INTEGER NOT NULL,
    parent_span_id TEXT,
    kind TEXT,
    name TEXT,
    seq INTEGER,
    data TEXT NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    unattached INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project, trace_id, id)
);
CREATE INDEX events_roots ON events (project, unattached, arrival)
WHERE type = 'start' AND parent_span_id IS NULL;
CREATE INDEX events_run_roots ON events (project, trace_id, seq, arrival)
WHERE type = 'start' AND parent_span_id IS NULL;
CREATE INDEX events_spans ON events (project, trace_id, span_id, type);
CREATE INDEX events_runs ON events (project, trace_id);
PRAGMA user_version = 2;
"""


def make_store_file_at_schema_version_2(database_path: Path, events: list[dict[str, Any]]) -> None:
    connection = sqlite3.connect(database_path)
    connection.executescript(STORE_FILE_AT_SCHEMA_VERSION_2)
    connection.executemany(
        "INSERT INTO events (project, trace_id, id, type, span_id, time_unix_nano,"
        " parent_span_id, kind, name, seq, data, created_at_unix_nano, unattached)"
        " VALUES ('default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        [
            (
                *(event.get(column) for column in COLUMNS_AT_SCHEMA_VERSION_2),
                json.dumps(
                    {
                        name: value
                        for name, value in event.items()
                        if name not in COLUMNS_AT_SCHEMA_VERSION_2
                    }
                ),
                "unattached" in event.get("flags", []),
            )
            for event in events
        ],
    )
    connection.commit()
    connection.close()


def test_a_store_file_from_before_data_was_kept_apart_reads_as_it_did(
    tmp_path: Path, serve_store: Any
) -> None:
    database_path = tmp_path / "older.db"
    make_store_file_at_schema_version_2(database_path, [*RECORDED_RUN, LOOSE_CALL])

    with serve_store(SqliteStorageEngine(database_path, time.time_ns)) as store:
        _, replay = store.get("/v1/runs/t7c1/events")
        _, loose = store.get("/v1/runs/t9/events")
        _, runs = store.get("/api/v1/runs")
        _, calls = store.get("/api/v1/unattached-calls")
        _, tree = store.get("/api/v1/runs/t7c1")
        _, call = store.get("/api/v1/runs/t7c1/calls/s3")
        store.post("/v1/events", {"events": [LATE_NOTE, RECORDED_RUN[0]]})
        _, grown = store.get(f"/v1/runs/t7c1/events?cursor={len(RECORDED_RUN)}")
    with serve_store(SqliteStorageEngine(database_path, time.time_ns)) as reopened:
        _, read_again = reopened.get("/v1/runs/t7c1/events")

    assert replay == {"events": RECORDED_RUN, "next_cursor": None}
    assert loose["events"] == [LOOSE_CALL]
    assert [
        (run["id"], run["status"], run["environment"], run["mode"], run["conditions"])
        for run in runs["runs"]
    ] == [("t7c1", "ok", "production", "normal", [])]
    assert [(call["id"], call["flags"]) for call in calls["runs"]] == [("t9", ["unattached"])]
    assert [child["kind"] for child in tree["run"]["root"]["children"]] == [
        "llm",
        "http",
        "function",
    ]
    assert call["call"]["response"]["body"]["json"] == {"id": "re_456", "status": "succeeded"}
    assert grown == {"events": [LATE_NOTE], "next_cursor": None}
    assert read_again["events"] == [*RECORDED_RUN, LATE_NOTE]
```

The events in the fixture carry `time_unix_nano` as a string. SQLite's `INTEGER` column stores it as a number, as the engine does.

- [ ] **Step 3: Run both guards against the code as it is**

Run: `uv run pytest packages/store/tests/test_replay_read.py packages/store/tests/test_older_store_files.py -v`
Expected: all PASS. They describe behaviour that must not change, so they pass before the change and after it. If one fails now, the guard is wrong: fix the guard before touching the engine.

- [ ] **Step 4: See the guards catch a mistake**

A guard that has only ever passed proves nothing. Temporarily change `json.dumps(event.fields)` in `add_events` to `json.dumps({**event.fields, "status": "changed"})`, run the first guard, and see the cases FAIL. Undo the change.

- [ ] **Step 5: Implement `stored_fields.py`, step 3, and the two-table engine**

- [ ] **Step 6: Run the store's tests**

Run: `uv run pytest packages/store -v`
Expected: all PASS, including the two existing older-file tests in `test_run_list.py` (a file from before schema versions, opened by six processes at once and listed), which now run steps 1, 2 and 3.

- [ ] **Step 7: Run the whole suite and the checks, and commit**

`git commit -m "Keep an event's data in its own table and give five fields columns"`. The message body says why: a read of the events table no longer touches a body.

### Task 3: The tree and the list read core columns only

**Files:**
- Modify: `packages/store/src/irimi_store/storage.py`, `sqlite_storage.py`, `reader_routes.py`

**Interfaces produced:**

```python
# irimi_store.storage.StorageEngine
def read_run_core_events(self, project: str, trace_id: str) -> tuple[Event, ...]: ...
```

Decisions the signatures leave open:

- A core event is an `Event`, `StartEvent` or `EndEvent` whose `fields` hold only those of `CORE_FIELD_NAMES` the event carries. Assembly already reads nothing else for a tree node, a list row or a condition, so `assembly.py` does not change.
- `read_run_core_events` returns every event of the run in arrival order from `events` alone. It is not paged: its rows are small, and the tree is not paged.
- `list_roots` and `_read_root` select from `events` alone, so `StoredRoot.start` and `StoredRoot.end` are core events. `SELECT_ROOTS` keeps its `WHERE`, its two subqueries and its order.
- `reader_routes.read_run` calls `storage.read_run_core_events` through `run_in_threadpool`. `_read_every_run_event` and `ASSEMBLY_PAGE_LIMIT` are deleted. An empty result is still `NotHeldError`.
- `read_call_events` and `read_run_events` still read whole events.

- [ ] **Step 1: Confirm what guards this task**

No new test: a reader cannot tell which columns were read, and a timing assertion would be flaky. The behaviour is guarded by the existing tests of the tree, the list and the conditions, and the cost is measured in Task 5. Run them first to see them pass: `uv run pytest packages/store/tests/test_run_tree.py packages/store/tests/test_run_list.py -v`.

- [ ] **Step 2: Implement**

- [ ] **Step 3: Check that no tree or list statement reads `event_data`**

Read `sqlite_storage.py`: the statements behind `read_run_core_events`, `list_roots` and `_read_root` name only `events`.

- [ ] **Step 4: Run the whole suite and the checks, and commit**

`git commit -m "Read only core columns for the run tree and the run list"`.

### Task 4: Bound a replay page by size

**Files:**
- Modify: `packages/store/src/irimi_store/pages.py`, `storage.py`, `sqlite_storage.py`, `app.py`
- Test: `packages/store/tests/test_replay_read.py`

**Interfaces produced:**

```python
# irimi_store.pages
def count_items_that_fit(sizes: Sequence[int], limit: int, size_limit: int) -> int: ...


# irimi_store.storage.StorageEngine
def read_run_events(
    self, project: str, trace_id: str, cursor: str | None, limit: int, size_limit: int
) -> Page[Event]: ...


# irimi_store.app
EVENT_PAGE_SIZE_LIMIT = 8 * 1024 * 1024
```

Decisions the signatures leave open:

- `count_items_that_fit` is pure. It returns how many of the leading `sizes` go in one page: the first always, then each next one while the count is under `limit` and the running total with it stays at or under `size_limit`. For no sizes it returns 0.
- `SqliteStorageEngine.read_run_events` runs two statements on one connection. The first reads `arrival, data_size` from `events` for the run after the cursor, in arrival order, `LIMIT limit + 1`. `count_items_that_fit` picks the count. The second reads the whole events with `arrival` after the cursor and up to the last picked `arrival`. `next_cursor` is that last `arrival` when the first statement returned more rows than were picked, else `None`.
- `app._read_event_page` passes `EVENT_PAGE_SIZE_LIMIT` in both of its calls. `limit` in the URL is read and refused as before.

- [ ] **Step 1: Write the failing test**

In `packages/store/tests/test_replay_read.py` (add `import json`):

```python
MEGABYTE = 1024 * 1024


def test_a_run_of_large_events_is_read_in_pages_bounded_by_size(store: Any) -> None:
    events = [
        {**RUN_NOTE, "id": f"n{number}", "text": "x" * (megabytes * MEGABYTE)}
        for number, megabytes in enumerate([3, 3, 3, 9, 1])
    ]
    for event in events:
        store.post("/v1/events", {"events": [event]})

    pages = [store.get("/v1/runs/t7c1/events")[1]]
    while pages[-1]["next_cursor"] is not None:
        pages.append(store.get(f"/v1/runs/t7c1/events?cursor={pages[-1]['next_cursor']}")[1])

    assert [event for page in pages for event in page["events"]] == events
    assert all(page["events"] for page in pages)
    assert max(len(json.dumps(page)) for page in pages if len(page["events"]) > 1) <= 8 * MEGABYTE
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest packages/store/tests/test_replay_read.py -v -k bounded_by_size`
Expected: FAIL on the last assertion. Today the five events come back as one page of about 19 MB.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the store's tests**

Run: `uv run pytest packages/store -v`
Expected: all PASS. `test_a_run_is_read_back_as_written_in_pages_that_hold_while_events_arrive` still passes unchanged: `limit=2` still gives two events.

- [ ] **Step 5: Run the whole suite and the checks, and commit**

`git commit -m "End a replay page before its events pass 8 MB"`.

### Task 5: Run the real thing

- [ ] **Step 1: Carry the measured database forward and measure again**

```
cp "$TMPDIR/irimi-14/before.db" "$TMPDIR/irimi-14/after.db"
.venv/bin/python "$TMPDIR/irimi-14/measure_reads.py" measure .venv/bin/irimi "$TMPDIR/irimi-14/after.db"
```

The first line, "store ready", now includes schema step 3 on a 1.07 GB file: about 4 s on the machine the spec was measured on, against 0.05 s before.

Expected after it:

- Both run list lines and both tree lines: a few milliseconds each, the same answer sizes as before, and store memory up by a few MB at most. The 320 MB tree no longer costs more than the 30 MB one.
- The call line: about as before.
- Both replay first-page lines: an answer of at most 8 MB, and store memory up by tens of MB, not hundreds.
- "replay read, long-run, every page": 82 answers, the largest about 4 MB.

If the list or a tree still grows with the bytes held, a statement is still reading `event_data`: stop and find it before going on.

- [ ] **Step 2: Check the carried database**

`sqlite3 "$TMPDIR/irimi-14/after.db" "PRAGMA user_version; SELECT COUNT(*) FROM events; SELECT COUNT(*) FROM event_data;"` prints `3`, `622` and `622`. `before.db` still prints `2`.

- [ ] **Step 3: Fill a new database with the new code and measure it**

```
.venv/bin/python "$TMPDIR/irimi-14/measure_reads.py" fill .venv/bin/irimi "$TMPDIR/irimi-14/new.db"
.venv/bin/python "$TMPDIR/irimi-14/measure_reads.py" measure .venv/bin/irimi "$TMPDIR/irimi-14/new.db"
```

Expected: the same lines as Step 1, with "store ready" back to a fraction of a second. Compare "filled in" with the time in `before.txt`: it shows what writing two rows per event costs.

- [ ] **Step 4: Check `irimi runs` against the carried database**

In one terminal: `.venv/bin/irimi store --port 7717 --database "$TMPDIR/irimi-14/after.db"`. In another:

```
export IRIMI_STORE_URL=http://127.0.0.1:7717
.venv/bin/irimi runs --limit 3
.venv/bin/irimi runs long-run
.venv/bin/irimi runs ticket-0 --json
```

Expected: three lines for `ticket-19`, `ticket-18`, `ticket-17`, each `handle_ticket` with status `ok` and no conditions, then the cursor line; a tree with `handle_ticket` and 40 `llm anthropic messages.create` children; and a JSON document whose root has ten children. Each returns at once. Stop the store.

- [ ] **Step 5: Open the PR**

`gh pr create` against `main` with `Closes #14`. The body holds the before and after output of the script, the time step 3 took, the "filled in" times, and the item the spec lists under "To confirm". Delete `$TMPDIR/irimi-14` afterwards.
