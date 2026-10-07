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

## End-to-End Tests

E2e tests in `tests/client/test_e2e.py` and `tests/sandbox/test_e2e.py` run against a live Baseten environment. They are skipped automatically when `BASETEN_E2E_TEST_API_KEY` is not set. The sandbox tests need no model, and print every request and response with `BASETEN_E2E_TEST_DEBUG=1`.

### Bootstrap

Deploy a minimal test model (requires `truss`):

```bash
BASETEN_E2E_TEST_API_KEY=... BASETEN_E2E_TEST_DOMAIN=... \
    uv run --with truss python -m scripts.e2e_test_bootstrap
```

This prints the model ID to stdout. Wait for the model to be ready before running tests.

### Running

```bash
BASETEN_E2E_TEST_API_KEY=... \
BASETEN_E2E_TEST_DOMAIN=... \
BASETEN_E2E_TEST_MODEL_ID=... \
    uv run poe test tests/test_e2e.py
```
