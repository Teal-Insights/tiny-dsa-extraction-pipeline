"""Emit a static startup site of public inputs, outputs, and the statement graph.

Loads the cached dependency graph plus binding sidecars the same way as
``scripts.binding_resolution_audit`` / ``scripts.internal_binding_burndown``.
Input rows come from ``derive_input_series`` and domain annotations compiled
from series bindings (with a ``CONSTRAINTS`` overlay). Output rows come from
``derive_output_series`` and include ``UNIT_MEASURE`` when present.

Run: ``uv run python -m scripts.i_o_tables``

Default output: ``artifacts/startup-site/``. Override with ``--output-dir``.
"""

from __future__ import annotations

import argparse
import csv
import html
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAliasType, cast, get_args, get_origin

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from excel_grapher.core.cell_types import (
    Between,
    RealBetween,
    normalize_cell_type_env_key,
)
from excel_grapher.exporter import to_semantic_viz_payload, write_semantic_viz_html
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_output_series,
    load_series_bindings,
)
from excel_grapher.series_bindings.domains import compile_domain_spec
from excel_grapher.series_bindings.ranges import expand_bound_series_addresses
from excel_grapher.series_bindings.relations import canonical_measure_dtype

from src.graph_cache import load_pipeline_dependency_graph
from src.pipeline_config import (
    PipelineConfig,
    load_pipeline_config,
    validate_pipeline_config,
)

DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts" / "startup-site"
INPUT_CSV_NAME = "startup-guide.csv"
OUTPUT_CSV_NAME = "output-catalog.csv"
INPUT_FIELDNAMES = (
    "series_id",
    "address",
    "description",
    "dtype",
    "acceptable_values",
    "default",
    "key",
)
OUTPUT_FIELDNAMES = (
    "series_id",
    "address",
    "compute",
    "unit_measure",
    "description",
    "key",
)
_INPUT_LABELS = (
    ("series_id", "Series"),
    ("address", "Address"),
    ("description", "Description"),
    ("dtype", "Dtype"),
    ("acceptable_values", "Acceptable values"),
    ("default", "Default"),
    ("key", "Key"),
)
_OUTPUT_LABELS = (
    ("series_id", "Series"),
    ("address", "Address"),
    ("compute", "Compute"),
    ("unit_measure", "Unit"),
    ("description", "Description"),
    ("key", "Key"),
)
_RuntimeLiteral: Any = cast(Any, Literal)
_PAGE_STYLE = """
:root { color-scheme: light; }
body { font-family: system-ui, sans-serif; margin: 1.5rem; line-height: 1.45; }
nav { margin-bottom: 1.25rem; }
nav a { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
th { background: #f4f4f4; }
""".strip()


@dataclass(frozen=True, slots=True)
class ConstraintFormat:
    dtype: str
    acceptable_values: str


def unwrap_annotation(annotation: object) -> object:
    """Resolve PEP 695 ``type`` aliases to their evaluated ``__value__``."""
    while isinstance(annotation, TypeAliasType):
        annotation = annotation.__value__
    return annotation


def format_constraint(
    constraint: object | None,
    *,
    fallback_dtype: str | None = None,
) -> ConstraintFormat:
    """Return dtype and acceptable values for Literal / Between / RealBetween."""
    fallback = fallback_dtype or ""
    if constraint is None:
        return ConstraintFormat(dtype=fallback, acceptable_values="")
    resolved = unwrap_annotation(constraint)
    origin = get_origin(resolved)
    if origin is Literal:
        values = get_args(resolved)
        return ConstraintFormat(
            dtype=_dtype_from_values(values) or fallback,
            acceptable_values=", ".join(_format_scalar(value) for value in values),
        )
    if origin is Annotated:
        args = get_args(resolved)
        base = args[0] if args else object
        dtype = _dtype_from_python_type(base) or fallback
        for metadata in args[1:]:
            if isinstance(metadata, RealBetween):
                return ConstraintFormat(
                    dtype=dtype or "float",
                    acceptable_values=_format_interval(metadata.min, metadata.max),
                )
            if isinstance(metadata, Between):
                return ConstraintFormat(
                    dtype=dtype or "int",
                    acceptable_values=_format_interval(metadata.min, metadata.max),
                )
        return ConstraintFormat(dtype=dtype, acceptable_values="")
    if isinstance(resolved, type):
        return ConstraintFormat(
            dtype=_dtype_from_python_type(resolved) or fallback,
            acceptable_values="",
        )
    return ConstraintFormat(dtype=fallback, acceptable_values="")


def domain_annotations_from_bindings(
    bindings: Mapping[str, Any],
    *,
    workbook: Path,
) -> dict[str, object]:
    """Return typing annotations equivalent to compiled series domains."""
    annotations: dict[str, object] = {}
    series_list = bindings.get("series", ())
    if not isinstance(series_list, list):
        return annotations
    for series in series_list:
        if not isinstance(series, Mapping):
            continue
        spec = compile_domain_spec(series)
        if spec is None or spec.get("from_workbook") is True:
            continue
        annotation = _annotation_from_domain_spec(spec)
        if annotation is None:
            continue
        for address in expand_bound_series_addresses(series, workbook=workbook):
            annotations[normalize_cell_type_env_key(address)] = annotation
            annotations[address] = annotation
    return annotations


def effective_domain_annotations(
    config: PipelineConfig,
    *,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Sidecar domain annotations with optional ``CONSTRAINTS`` overlay."""
    loaded = (
        bindings if bindings is not None else load_series_bindings(config.bindings_path)
    )
    annotations = domain_annotations_from_bindings(
        loaded, workbook=config.workbook_path
    )
    for key, value in config.constraints.items():
        annotations[normalize_cell_type_env_key(key)] = value
        annotations[key] = value
    return annotations


def input_catalog_rows(
    input_series: Sequence[Mapping[str, Any]],
    domain_annotations: Mapping[str, object],
    bindings: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build one catalog row per derived public-input cell."""
    series_by_id = _binding_series_by_id(bindings)
    rows: list[dict[str, str]] = []
    for series in input_series:
        series_id = str(series["id"])
        binding = series_by_id.get(series_id)
        fallback_dtype = canonical_measure_dtype(binding) if binding is not None else ""
        description = _series_notes(binding)
        for cell in series.get("cells") or []:
            if not isinstance(cell, Mapping):
                continue
            address = str(cell["address"])
            formatted = format_constraint(
                _annotation_for(address, domain_annotations),
                fallback_dtype=fallback_dtype,
            )
            record = cell.get("record")
            default = ""
            if isinstance(record, Mapping):
                default = _format_scalar(record.get("OBS_VALUE"))
            key = cell.get("key")
            rows.append(
                {
                    "series_id": series_id,
                    "address": address,
                    "description": description,
                    "dtype": formatted.dtype,
                    "acceptable_values": formatted.acceptable_values,
                    "default": default,
                    "key": _format_key(key if isinstance(key, Mapping) else {}),
                }
            )
    return rows


def output_catalog_rows(
    output_series: Sequence[Mapping[str, Any]],
    bindings: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build one catalog row per derived public-output cell."""
    series_by_id = _binding_series_by_id(bindings)
    rows: list[dict[str, str]] = []
    for series in output_series:
        series_id = str(series["id"])
        binding = series_by_id.get(series_id)
        description = _series_notes(binding)
        compute = str(series.get("compute_name") or "")
        for cell in series.get("cells") or []:
            if not isinstance(cell, Mapping):
                continue
            key = cell.get("key")
            rows.append(
                {
                    "series_id": series_id,
                    "address": str(cell["address"]),
                    "compute": compute,
                    "unit_measure": _unit_measure(cell, binding),
                    "description": description,
                    "key": _format_key(key if isinstance(key, Mapping) else {}),
                }
            )
    return rows


def posix_serve_directory(output_dir: Path, *, repo_root: Path) -> str:
    """Return a Git-Bash-safe ``--directory`` value (forward slashes)."""
    resolved = output_dir.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def emit_startup_site(
    config: PipelineConfig,
    *,
    output_dir: Path,
    graph: DependencyGraph | None = None,
) -> Path:
    """Write CSV catalogs, HTML pages, statement graph, and workbook download."""
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_graph = (
        graph if graph is not None else load_pipeline_dependency_graph(config)[0]
    )
    bindings = load_series_bindings(config.bindings_path)
    input_rows = input_catalog_rows(
        derive_input_series(resolved_graph, bindings, workbook=config.workbook_path),
        effective_domain_annotations(config, bindings=bindings),
        bindings,
    )
    output_rows = output_catalog_rows(
        derive_output_series(resolved_graph, bindings, workbook=config.workbook_path),
        bindings,
    )
    _write_csv(output_dir / INPUT_CSV_NAME, INPUT_FIELDNAMES, input_rows)
    _write_csv(output_dir / OUTPUT_CSV_NAME, OUTPUT_FIELDNAMES, output_rows)

    workbook_name = config.workbook_path.name
    download_dir = output_dir / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config.workbook_path, download_dir / workbook_name)

    payload = to_semantic_viz_payload(
        resolved_graph,
        bindings,
        workbook=config.workbook_path,
        blank_ranges=config.blank_ranges,
    )
    write_semantic_viz_html(
        payload,
        output_dir / "statement-graph.html",
        title=workbook_name,
    )

    (output_dir / "index.html").write_text(
        _index_page(
            input_count=len(input_rows),
            output_count=len(output_rows),
            workbook_name=workbook_name,
        ),
        encoding="utf-8",
    )
    (output_dir / "inputs.html").write_text(
        _catalog_page(
            title="Public inputs",
            labels=_INPUT_LABELS,
            rows=input_rows,
            csv_name=INPUT_CSV_NAME,
        ),
        encoding="utf-8",
    )
    (output_dir / "outputs.html").write_text(
        _catalog_page(
            title="Public outputs",
            labels=_OUTPUT_LABELS,
            rows=output_rows,
            csv_name=OUTPUT_CSV_NAME,
        ),
        encoding="utf-8",
    )
    return output_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for the startup site (default: artifacts/startup-site).",
    )
    args = parser.parse_args(argv)

    config = load_pipeline_config()
    validate_pipeline_config(config)
    output_dir = (
        args.output_dir
        if args.output_dir is not None
        else config.repo_root / "artifacts" / "startup-site"
    )
    emit_startup_site(config, output_dir=output_dir)
    served = posix_serve_directory(output_dir, repo_root=config.repo_root)
    print(f"Wrote startup site to {output_dir}")
    print(f"uv run python -m http.server 8000 --directory {served}")
    return 0


def _annotation_from_domain_spec(spec: Mapping[str, Any]) -> object | None:
    if "enum" in spec:
        return _RuntimeLiteral[tuple(spec["enum"])]
    if "between" in spec:
        bounds = spec["between"]
        if not isinstance(bounds, Mapping):
            raise TypeError(f"domain.between must be a mapping; got {bounds!r}")
        return Annotated[int, Between(bounds.get("min"), bounds.get("max"))]
    if "real_between" in spec:
        bounds = spec["real_between"]
        if not isinstance(bounds, Mapping):
            raise TypeError(f"domain.real_between must be a mapping; got {bounds!r}")
        return Annotated[float, RealBetween(bounds.get("min"), bounds.get("max"))]
    return None


def _binding_series_by_id(
    bindings: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for series in bindings.get("series") or []:
        if not isinstance(series, Mapping):
            continue
        series_id = series.get("id")
        if isinstance(series_id, str) and series_id and series_id not in indexed:
            indexed[series_id] = series
    return indexed


def _series_notes(series: Mapping[str, Any] | None) -> str:
    if series is None:
        return ""
    notes = series.get("notes")
    return str(notes) if notes else ""


def _annotation_for(address: str, annotations: Mapping[str, object]) -> object | None:
    if address in annotations:
        return annotations[address]
    return annotations.get(normalize_cell_type_env_key(address))


def _unit_measure(cell: Mapping[str, Any], series: Mapping[str, Any] | None) -> str:
    record = cell.get("record")
    if isinstance(record, Mapping):
        value = record.get("UNIT_MEASURE")
        if value not in (None, ""):
            return str(value)
    if series is None:
        return ""
    structure = series.get("structure")
    if not isinstance(structure, Mapping):
        return ""
    for attribute in structure.get("attributes") or []:
        if not isinstance(attribute, Mapping):
            continue
        if attribute.get("concept") != "UNIT_MEASURE":
            continue
        value = attribute.get("value")
        if value not in (None, ""):
            return str(value)
    return ""


def _format_key(key: Mapping[str, Any]) -> str:
    if not key:
        return ""
    return ", ".join(f"{name}={_format_scalar(value)}" for name, value in key.items())


def _format_scalar(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _format_interval(minimum: object, maximum: object) -> str:
    has_min = minimum is not None
    has_max = maximum is not None
    if has_min and has_max:
        return f"{minimum} ≤ x ≤ {maximum}"
    if has_min:
        return f"x ≥ {minimum}"
    if has_max:
        return f"x ≤ {maximum}"
    return ""


def _dtype_from_python_type(python_type: object) -> str:
    if python_type is bool:
        return "bool"
    if python_type is int:
        return "int"
    if python_type is float:
        return "float"
    if python_type is str:
        return "string"
    return ""


def _dtype_from_values(values: tuple[object, ...]) -> str:
    if not values:
        return ""
    if all(isinstance(value, bool) for value in values):
        return "bool"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return "int"
    if all(isinstance(value, float) for value in values):
        return "float"
    if all(isinstance(value, str) for value in values):
        return "string"
    return "string"


def _write_csv(
    path: Path,
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, str]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(fieldnames), lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def _nav() -> str:
    return (
        "<nav>"
        '<a href="index.html">Home</a>'
        '<a href="inputs.html">Inputs</a>'
        '<a href="outputs.html">Outputs</a>'
        '<a href="statement-graph.html">Statement graph</a>'
        "</nav>"
    )


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{_PAGE_STYLE}</style></head><body>{body}</body></html>\n"
    )


def _html_table(
    labels: Sequence[tuple[str, str]], rows: Sequence[Mapping[str, str]]
) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _key, label in labels)
    body_rows: list[str] = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape(row.get(key, ''))}</td>" for key, _label in labels
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        f"<table><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def _index_page(*, input_count: int, output_count: int, workbook_name: str) -> str:
    body = (
        f"{_nav()}"
        "<h1>Startup site</h1>"
        "<p>Browsable catalog of public inputs and outputs, plus the "
        "workbook and statement graph.</p>"
        "<ul>"
        f'<li><a href="inputs.html">Inputs</a> ({input_count})</li>'
        f'<li><a href="outputs.html">Outputs</a> ({output_count})</li>'
        '<li><a href="statement-graph.html">Statement graph</a></li>'
        f'<li><a href="download/{html.escape(workbook_name)}">Download {html.escape(workbook_name)}</a></li>'
        "</ul>"
        f'<p>CSV: <a href="{INPUT_CSV_NAME}">{INPUT_CSV_NAME}</a>, '
        f'<a href="{OUTPUT_CSV_NAME}">{OUTPUT_CSV_NAME}</a></p>'
    )
    return _page("Startup site", body)


def _catalog_page(
    *,
    title: str,
    labels: Sequence[tuple[str, str]],
    rows: Sequence[Mapping[str, str]],
    csv_name: str,
) -> str:
    body = (
        f"{_nav()}"
        f"<h1>{html.escape(title)}</h1>"
        f'<p><a href="{html.escape(csv_name)}">{html.escape(csv_name)}</a></p>'
        f"{_html_table(labels, rows)}"
    )
    return _page(title, body)


if __name__ == "__main__":
    raise SystemExit(main())
