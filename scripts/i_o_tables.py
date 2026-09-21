"""Build a static catalog of public inputs and outputs.

Loads the cached dependency graph plus binding sidecars, derives input and
output series, and writes CSV plus HTML pages under ``artifacts/startup-site``
(including a copy of the workbook and a statement-graph diagram).

Run: ``uv run python -m scripts.i_o_tables``
"""

from __future__ import annotations

import argparse
import csv
import html
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

from excel_grapher.core.cell_types import (
    Between,
    RealBetween,
    normalize_cell_type_env_key,
)
from excel_grapher.exporter import to_semantic_viz_payload, write_semantic_viz_html
from excel_grapher.grapher import DependencyGraph
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_output_series,
    load_series_bindings,
)
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.binding_domains import effective_domain_annotations
from src.graph_cache import load_pipeline_dependency_graph
from src.pipeline_config import (
    PipelineConfig,
    load_pipeline_config,
    validate_pipeline_config,
)

INPUT_COLUMNS = ("id", "range", "dtype", "dimensions", "acceptable", "notes")
OUTPUT_COLUMNS = ("id", "range", "dtype", "dimensions", "unit", "notes")
STATEMENT_GRAPH_NAME = "statement-graph.html"

_PAGE_STYLE = """
    body { font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
    table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
    th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
    th { background: #f4f4f4; }
    a.button { display: inline-block; margin: 1rem 0; padding: 0.6rem 1rem;
               background: #1a5f4a; color: #fff; text-decoration: none; border-radius: 4px; }
    nav a { margin-right: 1rem; }
    .diagram { width: 100%; height: 70vh; border: 1px solid #ccc; border-radius: 4px; }
"""

_NAV = """
  <nav>
    <a href="index.html">Overview</a>
    <a href="inputs.html">Inputs</a>
    <a href="outputs.html">Outputs</a>
  </nav>
"""

CatalogRow = dict[str, str | None]


def describe_constraint(annotation: object) -> tuple[str, str | None]:
    """Return ``(dtype, acceptable-values)`` for a domain annotation."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return type(args[0]).__name__, ", ".join(str(value) for value in args)
    if origin is Annotated:
        base, *meta = args
        for item in meta:
            if isinstance(item, (Between, RealBetween)):
                return getattr(base, "__name__", str(base)), f"[{item.min}, {item.max}]"
        return getattr(base, "__name__", str(base)), None
    return str(annotation), None


def _format_data_range(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _format_dimensions(meta: Mapping[str, Any]) -> str | None:
    dimensions = meta["structure"].get("dimensions", [])
    if not dimensions:
        return None
    return ", ".join(dimension["id"] for dimension in dimensions)


def _series_manifest(
    bindings: WorkbookSeriesBindings, direction: str
) -> dict[str, Mapping[str, Any]]:
    return {
        series["id"]: series for series in bindings["series"] if direction in series
    }


def _normalized_domain_env(
    config: PipelineConfig, bindings: WorkbookSeriesBindings
) -> dict[str, object]:
    annotations = effective_domain_annotations(config, bindings=bindings)
    return {
        normalize_cell_type_env_key(key): value for key, value in annotations.items()
    }


def input_catalog_rows(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    config: PipelineConfig,
) -> list[CatalogRow]:
    manifest = _series_manifest(bindings, "input")
    domains = _normalized_domain_env(config, bindings)
    rows: list[CatalogRow] = []
    for series in derive_input_series(graph, bindings, workbook=config.workbook_path):
        cells = series["cells"]
        if not cells:
            raise ValueError(f"input series {series['id']!r} resolved no cells")
        meta = manifest[series["id"]]
        domain = domains.get(normalize_cell_type_env_key(cells[0]["address"]))
        dtype, acceptable = (
            describe_constraint(domain)
            if domain is not None
            else (meta["structure"]["measure"]["dtype"], None)
        )
        rows.append(
            {
                "id": series["id"],
                "range": _format_data_range(meta.get("data_range")),
                "dtype": dtype,
                "dimensions": _format_dimensions(meta),
                "acceptable": acceptable,
                "notes": meta.get("notes"),
            }
        )
    return rows


def output_catalog_rows(
    graph: DependencyGraph,
    bindings: WorkbookSeriesBindings,
    *,
    config: PipelineConfig,
) -> list[CatalogRow]:
    manifest = _series_manifest(bindings, "output")
    rows: list[CatalogRow] = []
    for series in derive_output_series(graph, bindings, workbook=config.workbook_path):
        meta = manifest[series["id"]]
        attributes = {
            attribute["concept"]: attribute.get("value")
            for attribute in meta["structure"].get("attributes", [])
        }
        unit = attributes.get("UNIT_MEASURE")
        rows.append(
            {
                "id": series["id"],
                "range": _format_data_range(meta.get("data_range")),
                "dtype": meta["structure"]["measure"]["dtype"],
                "dimensions": _format_dimensions(meta),
                "unit": str(unit) if unit is not None else None,
                "notes": meta.get("notes"),
            }
        )
    return rows


def _write_catalog_csv(
    path: Path, rows: Sequence[CatalogRow], columns: Sequence[str]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) or "" for column in columns})


def _html_table(rows: Sequence[CatalogRow], columns: Sequence[str]) -> str:
    header = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body: list[str] = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape(row.get(column) or '')}</td>" for column in columns
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _page(*, title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>{_PAGE_STYLE}</style>
</head>
<body>
  {_NAV}
  {body}
</body>
</html>
"""


def serve_directory_arg(output_dir: Path, repo_root: Path) -> str:
    """Return a Git-Bash-safe ``--directory`` value for ``http.server``.

    Windows ``Path`` stringification uses backslashes; pasting that into Git Bash
    treats ``\\t`` as a tab and serves the wrong folder (``GET /`` 404).
    """
    resolved = output_dir.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def write_startup_site(config: PipelineConfig, output_dir: Path) -> Path:
    """Write CSV catalogs, HTML pages, the workbook copy, and the statement graph."""
    graph, _cache_key = load_pipeline_dependency_graph(config)
    bindings = load_series_bindings(config.bindings_path)
    inputs = input_catalog_rows(graph, bindings, config=config)
    outputs = output_catalog_rows(graph, bindings, config=config)

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_catalog_csv(output_dir / "startup-guide.csv", inputs, INPUT_COLUMNS)
    _write_catalog_csv(output_dir / "output-catalog.csv", outputs, OUTPUT_COLUMNS)

    payload = to_semantic_viz_payload(
        graph,
        bindings,
        workbook=config.workbook_path,
        blank_ranges=config.blank_ranges,
    )
    write_semantic_viz_html(
        payload,
        output_dir / STATEMENT_GRAPH_NAME,
        title=f"{config.dist_metadata.library_name} statement graph",
    )
    stats = payload.graph.stats
    print(
        f"wrote {output_dir / STATEMENT_GRAPH_NAME} "
        f"statements={stats.statement_count} "
        f"bundles={stats.bundle_count} "
        f"instance_edges={stats.instance_edge_count} "
        f"cells={stats.cell_count}"
    )

    download_name = config.workbook_path.name
    (output_dir / "download").mkdir(exist_ok=True)
    shutil.copy2(config.workbook_path, output_dir / "download" / download_name)

    title = config.dist_metadata.library_name
    description = config.dist_metadata.description
    escaped_download = html.escape(download_name)
    (output_dir / "index.html").write_text(
        _page(
            title=f"{title} — Overview",
            body=(
                f"<h1>{html.escape(title)}</h1>\n"
                f"  <p>{html.escape(description)}</p>\n"
                f'  <a class="button" href="download/{escaped_download}" download>'
                "Download spreadsheet</a>\n"
                "  <h2>Statement graph</h2>\n"
                f'  <iframe class="diagram" src="{STATEMENT_GRAPH_NAME}" '
                'title="Statement dependency graph"></iframe>\n'
                f'  <p><a href="{STATEMENT_GRAPH_NAME}">Open full-page diagram</a></p>'
            ),
        ),
        encoding="utf-8",
    )
    (output_dir / "inputs.html").write_text(
        _page(
            title=f"{title} — Inputs",
            body=(
                f"<h1>{html.escape(title)}</h1>\n"
                "  <p>Edit only the input cells listed below. Leave formulas alone.</p>\n"
                f'  <a class="button" href="download/{escaped_download}" download>'
                "Download spreadsheet</a>\n"
                "  <h2>Startup guide</h2>\n"
                f"  {_html_table(inputs, INPUT_COLUMNS)}"
            ),
        ),
        encoding="utf-8",
    )
    (output_dir / "outputs.html").write_text(
        _page(
            title=f"{title} — Outputs",
            body=(
                f"<h1>{html.escape(title)}</h1>\n"
                "  <h2>Output catalog</h2>\n"
                "  <p>Read-only results. Do not edit these cells in the spreadsheet.</p>\n"
                f"  {_html_table(outputs, OUTPUT_COLUMNS)}"
            ),
        ),
        encoding="utf-8",
    )
    print(output_dir)
    serve_dir = serve_directory_arg(output_dir, config.repo_root)
    print(f"Serve: uv run python -m http.server 8000 --directory {serve_dir}")
    return output_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for CSV/HTML output (default: artifacts/startup-site).",
    )
    args = parser.parse_args(argv)
    config = load_pipeline_config()
    validate_pipeline_config(config)
    output_dir = (
        args.output_dir
        if args.output_dir is not None
        else config.repo_root / "artifacts" / "startup-site"
    )
    write_startup_site(config, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
