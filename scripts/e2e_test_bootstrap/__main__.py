"""Bootstrap a minimal Truss model for e2e testing.

Reads from the environment:
  BASETEN_E2E_TEST_API_KEY — API key for authentication (required)
  BASETEN_E2E_TEST_DOMAIN  — Domain, e.g. staging.baseten.co (required)

Pushes a tiny echo model via truss as a library (without mutating
~/.trussrc) and prints the resulting model ID to stdout.

Usage:
    BASETEN_E2E_TEST_API_KEY=... BASETEN_E2E_TEST_DOMAIN=... \
        python -m scripts.e2e_test_bootstrap
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

CONFIG_YAML = """\
model_name: sdk-e2e-test
python_version: py312
resources:
  cpu: "1"
  memory: 2Gi
"""

MODEL_PY = """\
class Model:
    def load(self):
        pass

    def predict(self, request):
        return {"message": "ok"}
"""


def main() -> None:
    api_key = os.environ.get("BASETEN_E2E_TEST_API_KEY", "")
    if not api_key:
        print("BASETEN_E2E_TEST_API_KEY is required", file=sys.stderr)
        sys.exit(1)

    domain = os.environ.get("BASETEN_E2E_TEST_DOMAIN", "")
    if not domain:
        print("BASETEN_E2E_TEST_DOMAIN is required", file=sys.stderr)
        sys.exit(1)

    remote_url = f"https://app.{domain}"

    try:
        from truss.remote.baseten.remote import BasetenRemote  # type: ignore[import-untyped]
        from truss.truss_handle.build import load  # type: ignore[import-untyped]
    except ImportError:
        print(
            "truss is required: pip install truss (or uv pip install truss)",
            file=sys.stderr,
        )
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "config.yaml").write_text(CONFIG_YAML)
        model_dir = root / "model"
        model_dir.mkdir()
        (model_dir / "__init__.py").write_text("")
        (model_dir / "model.py").write_text(MODEL_PY)

        print(f"Pushing minimal Truss model to {remote_url}...", file=sys.stderr)

        # Use BasetenRemote directly to avoid mutating ~/.trussrc.
        remote = BasetenRemote(remote_url=remote_url, api_key=api_key)
        truss_handle = load(str(root))
        service = remote.push(
            truss_handle,
            model_name="sdk-e2e-test",
            working_dir=root,
            publish=True,
        )

        model_id = service.model_id
        print(f"Model ID: {model_id}", file=sys.stderr)
        # Print bare model ID to stdout for easy capture.
        print(model_id)


if __name__ == "__main__":
    main()
