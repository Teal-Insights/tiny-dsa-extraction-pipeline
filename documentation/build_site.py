"""Build demo assets and the standalone Great Docs site."""

from __future__ import annotations

import subprocess
from pathlib import Path

DOCUMENTATION_ROOT = Path(__file__).resolve().parent
REPO_ROOT = DOCUMENTATION_ROOT.parent
SITE_ROOT = DOCUMENTATION_ROOT / "great-docs" / "_site"


def build_site() -> Path:
    """Build the extraction-pipeline overview site."""
    subprocess.run(
        [
            "great-docs",
            "build",
            "--project-path",
            str(DOCUMENTATION_ROOT),
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    return SITE_ROOT


def main() -> int:
    """Build the site from the command line."""
    site_root = build_site()
    print(f"Built documentation site at {site_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
