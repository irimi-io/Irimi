# irimi

Shadow runs for AI agents. Record a workflow run in production, replay it with one change (a model,
a prompt, code) with every write faked, and see how the two runs differ.

```sh
pip install irimi
```

Early development: the rebuild is not released yet.

## Development

```sh
uv sync
uv run pytest
```

See `CLAUDE.md` for layout and conventions.
