"""Patch empty-IF ``None`` arms to ``0.0`` in a mechanical internals module.

Use this on salvaged or checkpointed mechanical modules produced before
excel-grapher 3.15.3 so batched parity matches ``xl_cell`` without
re-running Pass 1:

    uv run python -m scripts.patch_mechanical_empty_if \\
        .cache/mechanical-salvage.py \\
        --output dist/<package>/internals.mechanical.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.empty_if_rewrite import rewrite_empty_if_none_literals  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="Mechanical internals module to rewrite (e.g. .cache/mechanical-salvage.py)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Destination path (default: overwrite input in place)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the file would change; do not write",
    )
    args = parser.parse_args(argv)

    source = args.input.read_text(encoding="utf-8")
    rewritten = rewrite_empty_if_none_literals(source)
    if rewritten == source:
        print(f"unchanged: {args.input}")
        return 0
    if args.check:
        print(f"would rewrite: {args.input}", file=sys.stderr)
        return 1
    output = args.output if args.output is not None else args.input
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rewritten, encoding="utf-8", newline="\n")
    print(f"rewrote: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
