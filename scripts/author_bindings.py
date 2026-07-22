"""Emit binding sidecars from a declarative catalog and validate them.

This is the code-as-source-of-truth complement to
``templates/binding-authoring-prompt.txt`` for large, regular binding surfaces.
Keep workbook-specific geometry in a catalog YAML/JSON file and let each derived
repo specialize the catalog rather than forking the emission script.

Run:

    uv run python -m scripts.author_bindings

    uv run python -m scripts.author_bindings --catalog templates/binding-catalog.example.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.binding_authoring import emit_bindings_from_catalog  # noqa: E402
from src.pipeline_config import load_pipeline_config, validate_pipeline_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate binding sidecars from a declarative catalog."
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=REPO_ROOT / "templates" / "binding-catalog.example.yaml",
        help="Declarative catalog describing inputs/outputs/internals series.",
    )
    parser.add_argument(
        "--bindings-dir",
        type=Path,
        default=None,
        help="Directory for emitted *.bindings.yaml files (defaults to workbook config).",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Write YAML only; do not run validate_bindings_workbook.",
    )
    args = parser.parse_args()

    config = load_pipeline_config()
    validate_pipeline_config(config)
    bindings_dir = args.bindings_dir or config.bindings_path

    written, validation = emit_bindings_from_catalog(
        catalog_path=args.catalog,
        bindings_dir=bindings_dir,
        workbook_path=config.workbook_path,
        validate=not args.skip_validation,
    )
    for path in written:
        series_count = len(yaml.safe_load(path.read_text(encoding="utf-8"))["series"])
        print(f"Wrote {path} ({series_count} series)")

    if validation is not None:
        report = validation["report"]
        errors = [issue for issue in report["issues"] if issue["level"] == "error"]
        warnings = [issue for issue in report["issues"] if issue["level"] == "warning"]
        print(
            f"Validation ok={report['ok']} errors={len(errors)} "
            f"warnings={len(warnings)}"
        )
        print(
            f"Setters: {len(validation['setters'])} "
            f"Computes: {len(validation['computes'])} "
            f"Input series: {len(validation['input_series'])}"
        )


if __name__ == "__main__":
    main()
