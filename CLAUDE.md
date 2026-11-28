# irimi

Shadow runs for AI agents: record a workflow run in production, replay it with one change (a model,
a prompt, code) with writes faked, and compare the two runs. Python, managed with `uv`.

The spec lives in Notion, on the Irimi page (Scope, and Step 1: Decide the boundaries). When the
code and Notion disagree, ask which one is right; do not quietly pick one.

Decisions made during the build go on the Design Decisions page in Notion. Write each one as a
sentence or two saying what was decided, with the date, so it is cheap to replace later. No
rationale, alternatives, costs or consequences unless asked. Record only what Henry decided or
approved; mark anything else as proposed, and never write down details nobody discussed.

## Layout

A `uv` workspace of separate packages. Each has its own `pyproject.toml`, a `src/` layout and its
own `tests/`.

```
packages/
  sdk/        irimi: init, the decorators, HTTP interception, export to the store
  store/      the run store service: ingest, assembled runs, the replay read
  cli/        irimi runs | replay | diff, and starting a local store
```

- The SDK runs inside customers' production apps. It has as few dependencies as possible, never
  imports from `store` or `cli`, and supports older Python than the rest (3.10+; the rest 3.12+).
- Only the SDK is published, as `irimi` on PyPI, so `pip install irimi` is the whole install.
  `irimi-store` and `irimi-cli` are marked `Private :: Do Not Upload` until we decide how they
  ship.
- The CLI reaches the store only through the store's HTTP API, never through its storage.
- The wire format is defined once and imported by both sides. Never copy a message shape.
- New code goes in the package that owns the behaviour. Ask before adding a package.

## The three boundaries

1. User code to SDK: `init`, `@irimi.workflow`, `@irimi.function`, `flush`.
2. SDK to store: the wire format, versioned in the URL (`/v1/...`).
3. Store to readers: the CLI, users' own scripts, later the UI.

Each is versioned from day one and changes by addition only: unknown fields are kept or ignored,
never rejected. A change to the public SDK surface or the wire format needs an explicit OK first.
Everything behind a boundary is free to change.

Two rules that hold everywhere:

- In a normal run the SDK fails open. It never raises into the user's app and never blocks it.
- In a shadow run it fails closed. When unsure whether a call writes, fake it and flag it.

## Code

- **No comments.** Code must read clearly on its own: say it with names, small functions and types.
  If a comment feels necessary, rename or restructure instead. Docstrings appear only on the public
  SDK surface, because users read them in their editor. No commented-out code, no TODOs (open an
  issue).
- **Names carry the meaning.** Full words, no abbreviations. Functions are verbs, data is nouns,
  booleans read as questions (`is_replay`, `has_body`). The name says what something is, not how
  it is implemented.
- **Small and flat.** One job per function. Return early instead of nesting. Split a module before
  it holds two unrelated ideas.
- **Typed throughout.** Full annotations and `mypy --strict`. Data crossing a module boundary is a
  frozen dataclass or a typed model, never a loose `dict`. Closed sets are `Literal` or `Enum`.
- **Logic apart from I/O.** Decisions (matching, redaction, replay decisions) are pure functions
  over data. Network, storage, clock and environment stay at the edges and get passed in.
  One piece of logic decides how a call is replayed, whatever intercepted the call.
- **No junk drawers.** No `utils.py`, `helpers.py` or `common/`. A helper lives next to its only
  caller. When a second caller appears, it moves to a module named for what it does
  (`redaction.py`, `ids.py`).
- **No speculative code.** No config for a fixed value, no option nobody uses yet. Extract a shared abstraction only on the third repetition. Extending
  later is cheap. Removing a wrong abstraction is expensive.
- **Errors are explicit.** Raise specific exception types. No bare `except`. Never swallow an
  error silently. The SDK's fail-open path counts and flags what it drops.
- **State lives in one place.** No module-level mutable state, except the SDK's init state, which
  is held in one object.
- **Delete freely.** Remove dead code, unused parameters and stale branches when you see them.
- **Dependencies are a cost.** Adding one needs a reason stated in the PR. The SDK's needs an
  explicit OK.

## Tests

- Tests live in each package's `tests/`, never next to the code, and no test code ships in `src/`.
- Test behaviour through public interfaces: the SDK's API, the store's HTTP API, the CLI. Never
  test private functions. A refactor that changes no behaviour must not break a test.
- Keep them few and concise. One test per behaviour that matters, not one per function or per
  branch. Use `pytest.mark.parametrize` when cases differ only in data.
- End-to-end first: the main path (record, replay with an override, diff) has one real test that
  runs all three packages together. Add a unit test only where logic is dense (matching,
  redaction) or the end-to-end test cannot reach.
- Prefer real objects and small in-memory fakes to mocks. Never mock what you own.
- No network access and no API keys in tests.

## Commands

- `uv sync` to install the workspace.
- `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`,
  `uv run mypy --python-version 3.10 packages/sdk/src` and `uv run pytest` all pass before every
  commit. CI runs the same, then builds the SDK wheel and installs it with plain `pip` on 3.10 and
  3.14.
- Releases: bump `version` in `packages/sdk/pyproject.toml`, merge to `main`, push a tag
  `v<version>`. `release.yml` publishes to PyPI through trusted publishing.

## Git

- Branch per issue, `<issue-number>-<short-slug>`. PRs against `main` with `Closes #<n>`.
- Fix a minor problem found in a PR in that same PR. Do not open an issue for it.
- Small PRs, one concern each. Commit messages say what changed and why, in the imperative.
