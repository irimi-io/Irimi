# Record dataclasses and Pydantic models as their fields

Design note for issue #6. Written 2026-11-27.

Each choice is marked **decided** (Henry said so, in the issue or its comment of 2026-11-27) or
**settled here** (worked out from the code, inside what was decided). Anything that goes past
what was decided is listed again under "Beyond what was approved".

## What is recorded today

`irimi._serialisation.serialise` runs on the caller's thread for the arguments and the return
value of every decorated call. It keeps `None`, `bool`, `int`, finite `float`, whole `str`,
`list`, `tuple` (as a list) and `dict` with whole string keys. Every other value is replaced in
place by `{"$irimi": "unserialisable", "type": "<module>.<qualname>"}`. A dict that holds the key
`$irimi` is wrapped as `{"$irimi": "literal", "value": {...}}`.

When any value was replaced, the pass reports the result as not complete. Then:

- the root workflow's start event carries the flag `not_replayable` (input not complete);
- any call's end event carries `unserialisable_output` (output not complete).

The store computes the run condition `not_replayable` from that flag alone
(`irimi_store.assembly.read_conditions`). It never looks inside a value.

So `handle_ticket(ticket: Ticket)` with a dataclass or a Pydantic model records
`{"$irimi": "unserialisable", "type": "app.models.Ticket"}` and the run can never be replayed.

A value nested deeper than the interpreter's recursion limit, or one that contains itself, makes
the pass raise `RecursionError` inside itself; the whole value is then recorded by its type and
flagged. An exception anywhere else in the pass is caught by the decorator, counted, and the
event is not recorded.

## The change

A dataclass instance or a Pydantic model is recorded as (decided):

```json
{"$irimi": "object", "type": "app.models.Ticket", "fields": {"id": "T-1042", "text": "Charged twice"}}
```

Only recording is built. How a replay rebuilds the object, and what it does when the type is
gone or changed, belongs to piece 5 (decided). The SDK gains no dependency and never imports
Pydantic (decided).

Settled here:

- **`type`** is `type(value).__module__ + "." + type(value).__qualname__`, from the existing
  `name_type`, the same text the `unserialisable` marker and a recorded error already carry. It
  is recorded as Python gives it, including the forms that cannot be imported by name:
  `__main__.Ticket` (a script), `app.build.<locals>.Ticket` (a class made inside a function),
  `app.models.Box[int]` (a generic Pydantic model), and `app.models.Order.Line` (a nested class,
  where the text alone does not say where the module ends). Piece 5 decides what to do with them.
- **`fields`** holds each field's value after the same pass, so everything below applies at any
  depth: an object inside an object, a list or dict of objects, an object inside a plain dict.
  Each nested object carries its own marker and its own type.
- **`fields` is escaped like any other object.** A field named `$irimi` (possible only as a
  Pydantic extra field) gives `"fields": {"$irimi": "literal", "value": {...}}`. Markers are
  still found on the escaped form. `unescape_reserved_keys` is unchanged: it returns a marker as
  it is, so the fields inside an `object` marker stay in escaped form for the reader that
  rebuilds them.
- **A dataclass** is an instance for which `dataclasses.is_dataclass` is true. Its fields are
  `dataclasses.fields(value)`, each read with `getattr`. That covers frozen dataclasses,
  `slots=True` dataclasses, `init=False` fields and Pydantic's `pydantic.dataclasses.dataclass`.
  Class variables, `InitVar`s, properties and attributes set outside the fields are not
  recorded. `dataclasses.asdict` is not used: it deep-copies and drops the type of nested
  objects.
- **A dataclass class** (the type, not an instance) is not an object with fields. It is recorded
  by its type as today, as `builtins.type`.
- **A Pydantic model** is a value whose type has a callable `model_dump` and a dict
  `model_fields`. Its fields are read one level deep: `getattr(value, name)` for each name in
  `type(value).model_fields`, then the entries of `value.model_extra` when that is a dict
  (models with `extra="allow"`). `model_dump()` itself is not called. Measured against Pydantic
  2.14: `model_dump()` flattens nested models to plain dicts, drops the fields of a subclass
  held in a field annotated with its base class, leaves out `Field(exclude=True)` fields and
  adds computed fields. Its result is the model's serialised form, not its fields, and can be
  silently incomplete. Private attributes and computed fields are not recorded.
- **Pydantic v1 models** have no `model_dump`, so by the decided detection rule they are not
  objects with fields. They are recorded by their type and flagged, as today. The same goes for
  `pydantic.v1` models inside Pydantic 2.
- **A value that is already plain wins.** The new check runs only for a value the pass would
  otherwise record by its type, so a dataclass that subclasses `dict` or `list` is still
  recorded as that dict or list. This keeps the cost of plain values unchanged.
- **A field whose value is unserialisable** (a `datetime`, a database session) gets the
  `unserialisable` marker in place, inside `fields`. The rest of the object is recorded. The
  result is not complete, so the flags below are set exactly as for any unserialisable value.
- **A read that raises** (a `getattr` on a field, `dataclasses.fields`, `model_extra`) never
  reaches the app. The object is recorded by its type, in place, the rest of the event is
  recorded, the result is not complete, and the failure is counted: one warning is logged the
  first time and the next `irimi.flush()` returns `False`. Without this, the exception would be
  caught by the decorator and the whole start or end event lost.
- **Cycles and depth.** No cycle tracking is added. An object that contains itself, or objects
  nested past the recursion limit, end as they do today: `RecursionError` is caught at the top
  of the pass and the whole value is recorded by its type and flagged. For a call's arguments
  the whole value is the arguments, so the input reads
  `{"$irimi": "unserialisable", "type": "builtins.dict"}`. An object costs about three frames a
  level: with Python's default limit, 200 nested objects are recorded and 400 are not. A cycle
  costs about 0.5 ms on the caller's thread (measured), as a cyclic list does today.
- **The same object in two places** is recorded twice. The recording is a tree; identity is not
  kept.

## The flags afterwards

Nothing about the flags or the store changes. What changes is when the pass calls a result
complete.

| Workflow input or output | `not_replayable` (input) | `unserialisable_output` (output) |
| --- | --- | --- |
| dataclass or Pydantic model, every field plain or itself such an object | not set | not set |
| such an object with one unserialisable field, at any depth | set | set |
| such an object whose fields could not be read | set | set |
| such an object in a cycle or nested too deep | set | set |
| Pydantic v1 model, a dataclass class, any other object | set, as today | set, as today |

`not_replayable` absent now means "the input was captured in full". It does not promise that a
replay can rebuild the object; that is piece 5's condition to add if it needs one. Runs recorded
before this change keep their flag, because their data was never captured.

## Cost

The pass was measured with a throwaway benchmark (the plan holds the script), median of 7
rounds, Python 3.12.14, Apple M5 Pro. "After" is a prototype of this design kept outside the
repo.

| One argument | Before | After (prototype) |
| --- | --- | --- |
| plain dict, 6 fields, one nested dict | 3.0 µs | 3.1 µs |
| plain list of 200 message dicts | 125 µs | 128 µs |
| object recorded by its type | 1.4 µs | 1.6 µs |
| dataclass holding the same data as the plain dict | 1.4 µs (type only) | 4.4 µs |
| Pydantic model holding the same data | 1.4 µs (type only) | 5.2 µs |
| list of 200 three-field dataclasses | 39 µs (types only) | 229 µs |

The baseline for a small call is about 3 µs. Plain values cost the same after the change. An
object costs about one and a half times the same data as a plain dict, roughly 1.1 µs an object.

## Tests

Through the public SDK API, reading events from the fake store, in
`packages/sdk/tests/test_recording.py`:

- an object as a workflow's input and output is recorded as its fields, with no flag
  (parametrised: a frozen `slots` dataclass, a Pydantic model, a dataclass holding a model
  subclass with an extra field and a list of dataclasses, a model with an extra field named
  `$irimi`);
- an unserialisable field is marked in place and the run is flagged;
- a dataclass class and a dataclass that contains itself join the existing parametrised test of
  unserialisable input.

In `packages/sdk/tests/test_fail_open.py`, in a subprocess: an object whose field raises is
recorded by its type, the app's result is unchanged, and `flush` reports it.

In `packages/store/tests/test_record_a_run.py`: an app run in a subprocess passes a dataclass
and a Pydantic model to a workflow; the store's reader API returns the fields and lists the run
with no `not_replayable` condition.

Pydantic becomes a development dependency of the workspace, because these tests need a real
model. No test covers Pydantic v1: a v1 model is, to the pass, any object without `model_dump`,
which the existing test covers.

## Risks

- **Reading a field can run the app's code.** `getattr` on a dataclass mapped by an ORM can load
  a relationship from the database, on the caller's thread. An exception is contained; time
  spent is not. Today such an object is recorded by its type without touching it.
- **More is recorded than before.** Every field of an object now reaches the store, including a
  Pydantic field marked `exclude=True`. Redaction is not part of this piece. A `SecretStr` is
  not plain, so it is marked unserialisable and its value is not recorded.
- **Common field types are still unserialisable.** `datetime`, `UUID`, `Decimal`, `Enum`, `set`
  and `bytes` are replaced by a marker, so a model holding one is still `not_replayable`. This
  issue does not change that; it is the next thing users will meet.
- **Cost grows with the number of objects**, as the last row of the table shows. There is no
  size limit on a recorded value today and none is added.
- **The marker commits the wire to Python's type names** until piece 5 says how they are
  resolved.

## Beyond what was approved

The wire gains only the approved `object` marker. No public function, argument or return type of
the SDK changes. No runtime dependency is added. These points were settled by the author and are
listed so they can be reversed cheaply:

1. **Pydantic fields are read from `model_fields` and `model_extra`; `model_dump` only detects
   the model.** Proposed. The issue says "detected by its `model_dump` method" and does not say
   how fields are read. To reverse: `read_fields` returns `value.model_dump()` instead.
2. **A failed read is counted, so the next `flush()` returns `False`.** Proposed. It uses the
   existing count; `flush` keeps its signature. To reverse: drop `Payload.unread_objects` and
   the one call that reports it; the marker and the flag stay.
3. **A new dependency group `model-libraries` holding `pydantic`**, included in `dev` and
   installed by the `pip-install` CI job, which runs the SDK tests outside the workspace.
4. **Not built:** Pydantic v1 models, `attrs` classes, named tuples (still a list), enums, and
   the field types listed under Risks.
