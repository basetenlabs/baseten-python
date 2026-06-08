"""Generate the API documentation site with pydoctor.

Usage:
    python -m scripts.docgen

Renders HTML API docs for the ``baseten`` package into ``_site/api`` and writes
a ``_site/index.html`` that redirects there. The ``_site`` directory is not
committed; it is built on demand and published to GitHub Pages by CI.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
PACKAGE_DIR = REPO_ROOT / "baseten"
OUTPUT_DIR = REPO_ROOT / "_site"
API_DIR = OUTPUT_DIR / "api"

PROJECT_NAME = "Baseten Python SDK"
PROJECT_URL = "https://github.com/basetenlabs/baseten-python"

# Redirect the site root to the generated API docs.
INDEX_REDIRECT = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=./api/">
<link rel="canonical" href="./api/">
</head>
<body><a href="./api/">Baseten Python SDK API documentation</a></body>
</html>
"""


def main() -> None:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    API_DIR.mkdir(parents=True)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pydoctor",
            "--make-html",
            f"--html-output={API_DIR}",
            "--docformat=google",
            f"--project-name={PROJECT_NAME}",
            f"--project-url={PROJECT_URL}",
            str(PACKAGE_DIR),
        ],
        check=True,
    )

    (OUTPUT_DIR / "index.html").write_text(INDEX_REDIRECT, encoding="utf-8")


if __name__ == "__main__":
    main()
