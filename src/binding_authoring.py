"""Programmatic binding emission from declarative catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from excel_grapher.series_bindings.workflow import (
    BindingsCheckResult,
    validate_bindings_workbook,
)

BINDING_DIRECTIONS: tuple[str, ...] = (
    "inputs",
    "outputs",
    "internals",
    "constants",
)
BINDING_FILENAMES: dict[str, str] = {
    "inputs": "inputs.bindings.yaml",
    "outputs": "outputs.bindings.yaml",
    "internals": "internals.bindings.yaml",
    "constants": "constants.bindings.yaml",
}


class BindingCatalogError(ValueError):
    """Raised when a binding catalog is missing required structure."""


def load_binding_catalog(path: Path) -> dict[str, Any]:
    """Load a declarative binding catalog from YAML or JSON."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        import json

        loaded = json.loads(text)
    else:
        loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise BindingCatalogError(f"Binding catalog root must be a mapping: {path}")
    schema_version = loaded.get("schema_version")
    if not isinstance(schema_version, str):
        raise BindingCatalogError(
            f"Binding catalog must declare string schema_version: {path}"
        )
    return loaded


def build_binding_documents(catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Split a catalog into workbook binding sidecar documents by direction."""
    schema_version = catalog["schema_version"]
    workbook = catalog.get("workbook")
    concept_scheme = catalog.get("concept_scheme")
    documents: dict[str, dict[str, Any]] = {}
    for direction in BINDING_DIRECTIONS:
        section = catalog.get(direction, {})
        if section is None:
            section = {}
        if not isinstance(section, Mapping):
            raise BindingCatalogError(
                f"Catalog section {direction!r} must be a mapping if present"
            )
        series = section.get("series", [])
        if series is None:
            series = []
        if not isinstance(series, list):
            raise BindingCatalogError(
                f"Catalog section {direction!r}.series must be a list if present"
            )
        document: dict[str, Any] = {
            "schema_version": schema_version,
            "series": series,
        }
        if direction == "inputs" and concept_scheme is not None:
            document["concept_scheme"] = concept_scheme
        if isinstance(workbook, str):
            document["workbook"] = workbook
        documents[BINDING_FILENAMES[direction]] = document
    return documents


def write_binding_documents(
    bindings_dir: Path,
    documents: Mapping[str, Mapping[str, Any]],
) -> list[Path]:
    """Write binding sidecar YAML files and return the written paths."""
    bindings_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, document in documents.items():
        path = bindings_dir / filename
        path.write_text(
            yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        written.append(path)
    return written


def validate_generated_bindings(
    *,
    workbook_path: Path,
    bindings_dir: Path,
) -> BindingsCheckResult:
    """Validate emitted bindings against the configured workbook."""
    return validate_bindings_workbook(workbook_path, bindings_dir)


def emit_bindings_from_catalog(
    *,
    catalog_path: Path,
    bindings_dir: Path,
    workbook_path: Path,
    validate: bool = True,
) -> tuple[list[Path], BindingsCheckResult | None]:
    """Load a catalog, write binding sidecars, and optionally validate them."""
    catalog = load_binding_catalog(catalog_path)
    documents = build_binding_documents(catalog)
    written = write_binding_documents(bindings_dir, documents)
    validation: BindingsCheckResult | None = None
    if validate:
        validation = validate_generated_bindings(
            workbook_path=workbook_path,
            bindings_dir=bindings_dir,
        )
        report = validation["report"]
        errors = [issue for issue in report["issues"] if issue["level"] == "error"]
        if errors:
            preview = "\n".join(str(issue) for issue in errors[:5])
            raise BindingCatalogError(
                "Generated bindings failed validation:\n" + preview
            )
    return written, validation
