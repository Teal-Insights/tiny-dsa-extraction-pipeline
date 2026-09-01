"""Export Tiny DSA through excel-grapher inverted-tree codegen.

Uses the pipeline graph built with ``CONSTRAINTS`` (OFFSET/INDEX
``DynamicRefConfig``). Does not call ``excel-grapher bindings validate``.

Writes ``dist/<package>_inverted/`` so the default ctx package is left alone.

Run: ``uv run python -m scripts.export_inverted_tree``
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.extraction_pipeline import build_pipeline_graph
from src.inverted_tree_export import (
    generate_inverted_tree_modules,
    write_inverted_tree_package,
)
from src.pipeline_config import load_pipeline_config, validate_pipeline_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    config = load_pipeline_config()
    validate_pipeline_config(config)
    graph_result = build_pipeline_graph(config)
    modules = generate_inverted_tree_modules(
        config,
        graph=graph_result.graph,
        series_bindings=graph_result.series_bindings,
    )
    package_root = write_inverted_tree_package(config, modules)
    print(f"Wrote inverted-tree package to {package_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
