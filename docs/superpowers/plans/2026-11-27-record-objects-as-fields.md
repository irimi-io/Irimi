# Record Objects as Their Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dataclass instance or a Pydantic model passed to, or returned from, a decorated call is recorded as its fields under the marker `{"$irimi": "object", "type": "...", "fields": {...}}`, so a workflow written as `handle_ticket(ticket: Ticket)` is no longer flagged `not_replayable`.

**Architecture:** The serialisation pass (`irimi._serialisation`) already walks a value and replaces what it cannot keep with a marker. One new pure function, `read_fields`, says whether a value is an object with fields and reads them one level deep. The pass calls it only for a value it would otherwise record by its type, then walks the fields like any other object. A read that raises is contained in the pass, counted, and reported through the SDK's existing drop count.

**Tech Stack:** Python 3.10+ (SDK), standard library only in the SDK. `pydantic` as a development dependency of the workspace.

**Spec:** `docs/superpowers/specs/2026-11-27-record-objects-as-fields-design.md`. Read it, `CLAUDE.md` and `gh issue view 6 --comments` before any task.

**Scope:** The recording side only. Nothing here rebuilds an object, imports a type by name, or reads the marker back. That is piece 5.

**Where this was tried:** the code and tests below were run in a throwaway copy of the repo at commit `1c38008`: 263 tests passed, with `ruff`, `mypy` and `mypy --python-version 3.10` clean. The copy is gone; nothing of it is in the branch.

**Settled by the author, to list in the PR body:** the spec's "Beyond what was approved" has four points. This plan builds points 1 to 3 as proposed. Each can be reversed in one place, named beside it:

| Point | Task | To reverse |
| --- | --- | --- |
| 1 Pydantic fields read from `model_fields` and `model_extra`, not `model_dump()` | 1 | `read_fields` in `_object_fields.py` |
| 2 A failed read is counted, so the next `flush()` returns `False` | 2 | `Payload.unread_objects` and `Sdk.report_unread_objects` |
| 3 Dependency group `model-libraries`, installed by the `pip-install` CI job | 0 | `pyproject.toml`, `.github/workflows/ci.yml` |

## Global Constraints

- `CLAUDE.md` in full. In particular: no comments; docstrings only on the public SDK surface; full names; frozen dataclasses across module boundaries; `mypy --strict`; no `utils` or `helpers`; tests only through `irimi`'s public API, the store's HTTP API or the CLI; no network beyond the loopback address; no API keys.
- The SDK gains no dependency. `packages/sdk/pyproject.toml` keeps `dependencies = []`. No SDK module imports `pydantic`. A model is recognised by attributes of its type.
- The wire gains exactly one thing: the marker `{"$irimi": "object", "type": <text>, "fields": <object>}`. No new flag word, no new field name, no change to an existing marker.
- No public SDK function, argument or return type changes. `irimi/__init__.py` is not touched.
- Nothing the pass does may raise into the app. Reading an object's fields runs the app's code, so it happens inside `try` / `except Exception`.
- No `# type: ignore`.
- `import irimi` must still not load `urllib.request` or `importlib.metadata` (`test_importing_irimi_does_not_load_the_modules_only_sending_needs`). `dataclasses` is already imported by the SDK.
- Before every commit all of these pass: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run mypy --python-version 3.10 packages/sdk/src`, `uv run pytest`.
- Commit messages: imperative, what changed and why, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.
- One PR, from the existing branch `6-record-objects-as-fields`, against `main`, with `Closes #6`.

## Review Focus

1. **The cost of a plain value.** The new check sits after every existing check in `_Snapshot.take`, so a call whose arguments are plain JSON runs the same instructions as before. Task 4 measures it. A reordering that puts the object check first fails this.
2. **A read that raises.** A field whose `getattr` raises, on the input and on the output. The app gets its result, the event is recorded with the object by its type, `flush` returns `False`. (Task 2)
3. **`RecursionError` is not a failed read.** An object that contains itself still ends at the top of the pass, as a cyclic list does. The `except RecursionError: raise` before `except Exception` is what keeps it so. (Task 1)
4. **The marker is built from the escaped fields.** A Pydantic extra field named `$irimi` gives `"fields": {"$irimi": "literal", "value": {...}}`. (Task 1)
5. **A dataclass class is not an instance.** `dataclasses.is_dataclass` is true for both. (Task 1)

## File Structure

```
pyproject.toml                       modify: group model-libraries (pydantic), included in dev
uv.lock                              modify: by uv
.github/workflows/ci.yml             modify: the pip-install job installs the new group
packages/sdk/src/irimi/
  _wire/markers.py                   modify: mark_object
  _wire/__init__.py                  modify: export mark_object
  _object_fields.py                  create: read_fields
  _serialisation.py                  modify: take an object's fields, count failed reads
  _recording.py                      modify: Payload.unread_objects
  _labeled_calls.py                  modify: pass the count into the Payload
  _sdk.py                            modify: Sdk.report_unread_objects
  _decorators.py                     modify: report the count at the start and end of a call
packages/sdk/tests/
  test_recording.py                  modify: objects as fields, a field marked in place, two pins
  test_fail_open.py                  modify: an object whose fields cannot be read
packages/store/tests/
  test_record_a_run.py               modify: an app given objects, read through the reader API
```

Not touched: `packages/store/src`, `packages/cli/src`, the fixtures in `packages/sdk/tests/fixtures`, `irimi/__init__.py`.

---

### Task 0: Pydantic for the tests, and the baseline

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`

- [ ] **Step 1: Check the workspace**

Run: `git status --short && git log --oneline -1 && uv sync`
Expected: a clean tree on `6-record-objects-as-fields`, with the spec and this plan committed.

- [ ] **Step 2: Add the dependency group**

Run: `uv add --group model-libraries pydantic`, then add the group to `dev` by hand so `[dependency-groups]` reads:

```toml
dev = [
    "irimi",
    "irimi-store",
    "irimi-cli",
    "mypy>=1.18",
    "pytest>=8.4",
    "ruff>=0.14",
    { include-group = "http-clients" },
    { include-group = "model-libraries" },
]
```

Keep the version bound `uv add` wrote for `pydantic` (2.12 or later; it must install on Python 3.10 and 3.14). Run `uv sync`.

- [ ] **Step 3: Install the group where CI runs the SDK tests outside the workspace**

In `.github/workflows/ci.yml`, job `pip-install`, change the one line:

```yaml
          "$RUNNER_TEMP/venv/bin/pip" install pytest --group http-clients --group model-libraries
```

- [ ] **Step 4: Write the benchmark in a temporary directory and record the baseline**

The benchmark is not committed. Create it outside the repo, for example with `BENCH=$(mktemp -d)` and the file `$BENCH/benchmark_serialisation.py`:

```python
import statistics
import sys
import timeit
from dataclasses import dataclass, field

from irimi._serialisation import serialise_arguments
from pydantic import BaseModel


@dataclass
class Customer:
    id: str
    email: str
    plan: str


@dataclass
class Ticket:
    id: str
    text: str
    amount: int
    tags: list[str]
    customer: Customer
    notes: list[str] = field(default_factory=list)


class Session:
    pass


class CustomerModel(BaseModel):
    id: str
    email: str
    plan: str


class TicketModel(BaseModel):
    id: str
    text: str
    amount: int
    tags: list[str]
    customer: CustomerModel
    notes: list[str] = []


CUSTOMER = {"id": "C-7", "email": "ada@example.com", "plan": "team"}
TICKET = {
    "id": "T-1042",
    "text": "I was charged twice for my subscription this month.",
    "amount": 1250,
    "tags": ["billing", "refund"],
    "customer": CUSTOMER,
    "notes": [],
}
MESSAGES = [{"role": "user", "content": "x" * 200, "index": index} for index in range(200)]
CASES = {
    "plain dict, 6 fields, one nested": (TICKET,),
    "plain list of 200 messages": (MESSAGES,),
    "dataclass, same data": (Ticket(**{**TICKET, "customer": Customer(**CUSTOMER)}),),
    "pydantic model, same data": (TicketModel(**TICKET),),
    "object recorded by its type": (Session(),),
    "list of 200 dataclasses": ([Customer(**CUSTOMER) for _ in range(200)],),
}


def measure_microseconds(arguments):
    timer = timeit.Timer(lambda: serialise_arguments(arguments, {}))
    loops, _ = timer.autorange()
    return statistics.median(timer.repeat(repeat=7, number=loops)) / loops * 1e6


print(f"python {sys.version.split()[0]}")
for name, arguments in CASES.items():
    is_complete = serialise_arguments(arguments, {}).is_complete
    print(f"{name:36} {measure_microseconds(arguments):9.2f} us per call   complete={is_complete}")
```

Run it twice from the repo root, on an otherwise idle machine: `uv run python "$BENCH/benchmark_serialisation.py"`. Keep the output for the PR body.

Baseline measured 2026-11-27 at commit `1c38008`, Python 3.12.14, Apple M5 Pro (yours will differ in absolute terms; compare before and after on one machine):

```
plain dict, 6 fields, one nested          3.02 us per call   complete=True
plain list of 200 messages              123.27 us per call   complete=True
dataclass, same data                      1.37 us per call   complete=False
pydantic model, same data                 1.44 us per call   complete=False
object recorded by its type               1.38 us per call   complete=False
list of 200 dataclasses                  39.32 us per call   complete=False
```

- [ ] **Step 5: Run the checks and commit**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run mypy --python-version 3.10 packages/sdk/src && uv run pytest`
Expected: all pass, nothing changed in behaviour.

`git add pyproject.toml uv.lock .github/workflows/ci.yml && git commit -m "Add Pydantic as a development dependency for tests of recorded models"`

The PR body states the reason, as `CLAUDE.md` asks: the tests record a real Pydantic model, and the SDK itself still depends on nothing.

### Task 1: An object is recorded as its fields

**Files:**
- Modify: `packages/sdk/src/irimi/_wire/markers.py`, `packages/sdk/src/irimi/_wire/__init__.py`
- Create: `packages/sdk/src/irimi/_object_fields.py`
- Modify: `packages/sdk/src/irimi/_serialisation.py`
- Test: `packages/sdk/tests/test_recording.py`

**Interfaces produced:**

```python
# irimi._wire.markers, exported from irimi._wire (import and __all__, in alphabetical place)
def mark_object(type_name: str, fields: JsonValue) -> dict[str, JsonValue]:
    return {RESERVED_KEY: "object", "type": type_name, "fields": fields}


# irimi._object_fields
def read_fields(value: object) -> dict[str, object] | None: ...
```

`read_fields` returns `None` for a value that is not an object with fields. It may raise whatever the app's own attribute access raises; its one caller contains that.

```python
from __future__ import annotations

import dataclasses


def read_fields(value: object) -> dict[str, object] | None:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    model_type = type(value)
    model_fields = getattr(model_type, "model_fields", None)
    if not callable(getattr(model_type, "model_dump", None)) or not isinstance(model_fields, dict):
        return None
    fields = {name: getattr(value, name) for name in model_fields}
    extra_fields = getattr(value, "model_extra", None)
    return {**fields, **extra_fields} if isinstance(extra_fields, dict) else fields
```

Decisions the signatures leave open:

- `model_dump` and `model_fields` are looked up on the type, never the instance. An instance lookup on a mock or a proxy object answers anything.
- `model_dump` is never called. See the spec, "A Pydantic model".
- In `_serialisation.py`, `_Snapshot.take` keeps every existing branch in its order. Only its last line changes, from `return self._record_by_type(value)` to `return self._take_fields(value)`:

```python
    def _take_fields(self, value: object) -> JsonValue:
        try:
            fields = read_fields(value)
        except RecursionError:
            raise
        except Exception:
            return self._record_by_type(value)
        if fields is None or not _has_whole_names(fields):
            return self._record_by_type(value)
        type_name = name_type(type(value))
        self.approximate_size += len(type_name) + SIZE_OF_A_SMALL_VALUE
        return mark_object(type_name, self._take_object(fields))
```

- `_has_whole_names(items: dict[str, object]) -> bool` is the existing test on a dict's keys (`all(isinstance(name, str) and _is_whole_text(name) for name in items)`), moved into a module-level function because it now has two callers: the dict branch of `take` and `_take_fields`.
- The fields go through the existing `_take_object`, which walks each value with `take` and escapes the key `$irimi`. Nothing else is needed for nested objects, lists of objects or the reserved key.
- The recursion into the fields is outside the `try`. Only reading the fields is the app's code.
- In this task a read that raises is recorded by its type and not counted. Task 2 adds the count; the bare `except Exception` branch here is its starting point, not the finished behaviour.
- `serialise`, `serialise_arguments`, `name_type` and the `RecursionError` handling in `serialise` do not change in this task.

- [ ] **Step 1: Write the failing tests**

In `packages/sdk/tests/test_recording.py`, add the imports `from dataclasses import dataclass` and `from pydantic import BaseModel, ConfigDict`, and below `class Session`:

```python
@dataclass(frozen=True, slots=True)
class Customer:
    id: str
    email: str


class Ticket(BaseModel):
    id: str
    tags: list[str]


class OpenTicket(Ticket):
    model_config = ConfigDict(extra="allow")
    urgency: int


@dataclass
class Refund:
    ticket: Ticket
    customers: list[Customer]
    amount: int
    reason: object = None


@dataclass
class Step:
    name: str
    next_step: object = None


def repeat_a_step_forever() -> Step:
    step = Step("retry")
    step.next_step = step
    return step


def as_object(type_name: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"$irimi": "object", "type": f"{__name__}.{type_name}", "fields": fields}


ADA = Customer(id="C-7", email="ada@example.com")
RECORDED_ADA = as_object("Customer", {"id": "C-7", "email": "ada@example.com"})
```

Below the `open_session` workflow:

```python
@irimi.workflow
def hand_back(value: object) -> object:
    return value
```

Two new tests, placed after `test_a_run_with_unserialisable_input_is_recorded_and_flagged`:

```python
@pytest.mark.parametrize(
    ("value", "recorded"),
    [
        (ADA, RECORDED_ADA),
        (
            Ticket(id="T-1042", tags=["billing"]),
            as_object("Ticket", {"id": "T-1042", "tags": ["billing"]}),
        ),
        (
            Refund(
                ticket=OpenTicket.model_validate(
                    {"id": "T-1042", "tags": [], "urgency": 2, "note": "call back"}
                ),
                customers=[ADA],
                amount=1250,
            ),
            as_object(
                "Refund",
                {
                    "ticket": as_object(
                        "OpenTicket",
                        {"id": "T-1042", "tags": [], "urgency": 2, "note": "call back"},
                    ),
                    "customers": [RECORDED_ADA],
                    "amount": 1250,
                    "reason": None,
                },
            ),
        ),
        (
            OpenTicket.model_validate({"id": "T-1042", "tags": [], "urgency": 2, "$irimi": "kept"}),
            as_object(
                "OpenTicket",
                {
                    "$irimi": "literal",
                    "value": {"id": "T-1042", "tags": [], "urgency": 2, "$irimi": "kept"},
                },
            ),
        ),
    ],
)
def test_a_dataclass_or_pydantic_model_is_recorded_as_its_fields(
    recorded_events: TakeEvents, value: object, recorded: dict[str, Any]
) -> None:
    assert hand_back(value) is value

    run_start, run_end = recorded_events()

    assert parse_batch({"events": [run_start, run_end]}).refused == ()
    assert run_start["input"] == {"args": [recorded], "kwargs": {}}
    assert run_end["output"] == recorded
    assert "flags" not in run_start
    assert "flags" not in run_end


def test_an_unserialisable_field_is_marked_in_place_and_the_run_is_flagged(
    recorded_events: TakeEvents,
) -> None:
    hand_back(Refund(ticket=Ticket(id="T-1", tags=[]), customers=[], amount=5, reason=Session()))

    run_start, run_end = recorded_events()

    assert run_end["output"]["type"] == f"{__name__}.Refund"
    assert run_end["output"]["fields"]["amount"] == 5
    assert run_end["output"]["fields"]["reason"] == {
        "$irimi": "unserialisable",
        "type": f"{__name__}.Session",
    }
    assert run_start["flags"] == ["not_replayable"]
    assert run_end["flags"] == ["unserialisable_output"]
```

The four cases cover: a frozen `slots` dataclass; a Pydantic model; a dataclass holding a model subclass in a field annotated with its base, an extra field and a list of dataclasses; an extra field named `$irimi`. The models with extra fields are built with `model_validate` because `mypy --strict` refuses an unknown keyword argument to a model.

Add two cases to the parameters of the existing `test_a_run_with_unserialisable_input_is_recorded_and_flagged`:

```python
        (Customer, "builtins.type"),
        (repeat_a_step_forever(), f"{__name__}.Step"),
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest packages/sdk/tests/test_recording.py -v`
Expected: the four cases of `test_a_dataclass_or_pydantic_model_is_recorded_as_its_fields` FAIL (the input holds `{"$irimi": "unserialisable", ...}`), and `test_an_unserialisable_field_is_marked_in_place_and_the_run_is_flagged` FAILS with `KeyError: 'fields'`. The two cases added to the existing test PASS already: they pin what must not change (a dataclass class, and an object that contains itself, stay recorded by their type and flagged).

- [ ] **Step 3: Implement `mark_object`, `read_fields` and `_take_fields`**

- [ ] **Step 4: Run the whole suite and the checks**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy && uv run mypy --python-version 3.10 packages/sdk/src`
Expected: all pass, including every other test in `test_recording.py` and both fixture round trips in `test_wire.py` unchanged.

- [ ] **Step 5: Commit**

`git commit -m "Record a dataclass or Pydantic model as its fields so its run can be replayed"`

### Task 2: A read that raises is contained and counted

**Files:**
- Modify: `packages/sdk/src/irimi/_serialisation.py`, `_recording.py`, `_labeled_calls.py`, `_sdk.py`, `_decorators.py`
- Test: `packages/sdk/tests/test_fail_open.py`

**Interfaces:**
- Consumes: `_take_fields` from Task 1.
- Produces:

```python
# irimi._serialisation
@dataclass(frozen=True)
class Serialised:
    value: JsonValue
    is_complete: bool
    approximate_size: int
    unread_objects: int


# irimi._recording
@dataclass(frozen=True)
class Payload:
    fields: Mapping[str, JsonValue]
    approximate_size: int
    unread_objects: int = 0


# irimi._sdk.Sdk
def report_unread_objects(self, count: int) -> None: ...
```

Decisions the signatures leave open:

- `_Snapshot` gains `self.unread_objects = 0`. The `except Exception` branch of `_take_fields` adds one before recording the object by its type. `serialise` passes the count into `Serialised`; its `RecursionError` branch passes `0`.
- `describe_start` and `describe_output` in `_labeled_calls.py` pass the count on: `Payload(fields, call_input.approximate_size, call_input.unread_objects)` and the same for the output. `_errors.py` and `_outbound_calls.py` build a `Payload` without it and are not touched.
- `Sdk.report_unread_objects` returns at once for `0`. Otherwise it calls `self._drops.record(count, "irimi could not read the fields of an object, so it is recorded by its type; the app is unaffected")`. The guard matters: `DropTally.record` logs its warning on the first call, whatever the count.
- `_decorators._begin_call` calls `SDK.report_unread_objects(payload.unread_objects)` between `describe_start` and `begin_call`, inside the existing `try`. `_end_call` does the same between `describe_end(outcome)` and `end_call`.
- Nothing is logged at the point of the failure and the exception's text is not kept. The marker in the event names the type.

- [ ] **Step 1: Write the failing test**

In `packages/sdk/tests/test_fail_open.py`, after `test_a_call_that_could_not_be_recorded_counts_as_not_sent`:

```python
OBJECT_THAT_CANNOT_BE_READ = """
from dataclasses import dataclass

@dataclass
class Ticket:
    id: str

    def __getattribute__(self, name):
        if name == "id":
            raise RuntimeError
        return super().__getattribute__(name)

@irimi.workflow
def keep(ticket, note):
    return note

print(keep(Ticket("T-1"), "kept"))
print(irimi.flush())
"""


def test_an_object_whose_fields_cannot_be_read_is_recorded_by_its_type_and_counted(
    fake_store: Any,
) -> None:
    output = run_app(
        f"irimi.init(store_url={fake_store.url!r})", teardown=OBJECT_THAT_CANNOT_BE_READ
    )

    run_start = next(event for event in fake_store.take_events() if event.get("name") == "keep")

    assert output == "ch_123\nkept\nFalse\n"
    assert run_start["input"]["args"] == [
        {"$irimi": "unserialisable", "type": "__main__.Ticket"},
        "kept",
    ]
    assert run_start["flags"] == ["not_replayable"]
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest packages/sdk/tests/test_fail_open.py -v -k cannot_be_read`
Expected: FAIL on `assert output == ...`, with `True` printed where `False` is expected: after Task 1 the object is already recorded by its type, and nothing counts it.

- [ ] **Step 3: Implement the count**

- [ ] **Step 4: Run the whole suite and the checks**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy && uv run mypy --python-version 3.10 packages/sdk/src`
Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -m "Count an object whose fields cannot be read instead of losing its event"`

### Task 3: The store returns the fields and calls the run replayable

**Files:**
- Test: `packages/store/tests/test_record_a_run.py`

No source file changes. The store keeps a value as written and computes `not_replayable` from the flag alone; this test pins that end to end.

- [ ] **Step 1: Write the test**

Append to `packages/store/tests/test_record_a_run.py`:

```python
APP_GIVEN_OBJECTS = """
from dataclasses import dataclass

import irimi
from pydantic import BaseModel

irimi.init()

@dataclass(frozen=True)
class Customer:
    id: str

class Ticket(BaseModel):
    id: str
    text: str

@irimi.workflow
def handle_ticket(ticket, customer):
    return customer

handle_ticket(Ticket(id="T-1042", text="Charged twice"), customer=Customer(id="C-7"))
"""


def test_a_run_given_objects_arrives_with_their_fields_and_is_replayable(store: Any) -> None:
    subprocess.run(
        [sys.executable, "-c", APP_GIVEN_OBJECTS],
        env={**os.environ, "IRIMI_STORE_URL": store.url},
        timeout=60,
        check=True,
    )

    (run,) = store.get("/api/v1/runs")[1]["runs"]
    call = store.get(f"/api/v1/runs/{run['id']}/calls/{run['span_id']}")[1]["call"]
    customer = {"$irimi": "object", "type": "__main__.Customer", "fields": {"id": "C-7"}}

    assert (run["flags"], run["conditions"]) == ([], [])
    assert call["input"] == {
        "args": [
            {
                "$irimi": "object",
                "type": "__main__.Ticket",
                "fields": {"id": "T-1042", "text": "Charged twice"},
            }
        ],
        "kwargs": {"customer": customer},
    }
    assert call["output"] == customer
```

- [ ] **Step 2: Run it**

Run: `uv run pytest packages/store/tests/test_record_a_run.py -v`
Expected: PASS. This test checks what Tasks 1 and 2 built through the store; it is not written failing-first. At the commit before Task 1 the same assertions fail with `run["flags"] == ["not_replayable", "unserialisable_output"]` and `run["conditions"] == ["not_replayable"]`.

- [ ] **Step 3: Run the whole suite and the checks, and commit**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy && uv run mypy --python-version 3.10 packages/sdk/src`

`git commit -m "Check that a run given objects reads back with their fields"`

### Task 4: Measure the cost again

**Files:** none in the repo.

- [ ] **Step 1: Run the Task 0 benchmark again, twice, on the same machine**

Run: `uv run python "$BENCH/benchmark_serialisation.py"`

Measured with a prototype of this plan on the baseline's machine:

```
plain dict, 6 fields, one nested          3.09 us per call   complete=True
plain list of 200 messages              125.39 us per call   complete=True
dataclass, same data                      4.35 us per call   complete=True
pydantic model, same data                 5.09 us per call   complete=True
object recorded by its type               1.55 us per call   complete=False
list of 200 dataclasses                 229.14 us per call   complete=True
```

- [ ] **Step 2: Compare with the baseline**

Expected:

- the two plain rows are within 5% of the baseline;
- "object recorded by its type" grows by no more than about 0.3 µs (the cost of asking whether it has fields);
- the dataclass and model rows are within twice the plain dict holding the same data;
- the three object rows now say `complete=True`.

If a plain row moved by more than run-to-run noise, the object check is in the wrong place in `take`. Fix that; do not accept it.

- [ ] **Step 3: Put both tables in the PR body**, with the Python version and machine.

### Task 5: Run the real thing

**Files:** none in the repo. The script lives in the temporary directory.

- [ ] **Step 1: Write `$BENCH/real_app.py`**

```python
from dataclasses import dataclass

import irimi
from pydantic import BaseModel


@dataclass(frozen=True)
class Customer:
    id: str
    email: str


class Ticket(BaseModel):
    id: str
    text: str
    tags: list[str]


@dataclass
class Refund:
    ticket: Ticket
    customer: Customer
    amount: int


irimi.init()


@irimi.function(write=True)
def record_refund(refund: Refund) -> dict[str, int]:
    return {"row_id": 9917}


@irimi.workflow
def handle_ticket(ticket: Ticket, customer: Customer) -> Refund:
    refund = Refund(ticket=ticket, customer=customer, amount=1250)
    record_refund(refund)
    return refund


handle_ticket(
    Ticket(id="T-1042", text="Charged twice", tags=["billing"]),
    customer=Customer(id="C-7", email="ada@example.com"),
)
print(irimi.flush())
```

- [ ] **Step 2: Record to a local store and read it back through the reader API**

In one terminal: `uv run irimi store --port 0 --database "$BENCH/store.db"`. Its first line is the store's URL.

In another, with `URL` set to that line:

```
IRIMI_STORE_URL=$URL uv run python "$BENCH/real_app.py"
curl -s "$URL/api/v1/runs"
curl -s "$URL/api/v1/runs/<id>/calls/<span_id>"
IRIMI_STORE_URL=$URL uv run irimi runs
```

`<id>` and `<span_id>` are the run's `id` and `span_id` in the first answer.

Expected:

- the script prints `True`;
- the listed run has `"flags": []` and `"conditions": []`;
- the call's `input` is

  ```json
  {"args": [{"$irimi": "object", "type": "__main__.Ticket", "fields": {"id": "T-1042", "text": "Charged twice", "tags": ["billing"]}}], "kwargs": {"customer": {"$irimi": "object", "type": "__main__.Customer", "fields": {"id": "C-7", "email": "ada@example.com"}}}}
  ```

- the call's `output` is a `__main__.Refund` object whose `ticket` and `customer` fields are those two objects and whose `amount` is `1250`;
- `irimi runs` ends the run's line with `ok  -`, where before this change it ended with `ok  not_replayable`.

Before this change the same script gave `"flags": ["not_replayable", "unserialisable_output"]` and an input of two `unserialisable` markers.

- [ ] **Step 3: Stop the store and remove the temporary directory**

- [ ] **Step 4: Open the PR**

`gh pr create` against `main`, from `6-record-objects-as-fields`. The body has: `Closes #6`; what is recorded now, in two sentences; the reason for the `pydantic` development dependency; the before and after benchmark tables; what Task 5 showed; and the three points from "Settled by the author" at the top of this plan, each marked proposed, with the spec's "Not built" list and its Risks section linked.
