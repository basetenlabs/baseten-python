# Contributing

## Setup

```bash
uv sync
```

## Tasks

All tasks are run via `uv run poe <task>`:

- `generate-api` - Regenerate API clients and models from OpenAPI specs (pass `--update-specs` to download latest specs first)
- `format` - Format code and auto-fix lint issues
- `lint` - Check formatting and lint (fails on issues)
- `typecheck` - Run type checker
- `test` - Run tests
