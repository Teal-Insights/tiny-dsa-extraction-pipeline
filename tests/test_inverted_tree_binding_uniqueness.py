"""Inverted-tree catalog requires unique cell ownership across binding shards."""

from __future__ import annotations

from typing import cast

import pytest
from excel_grapher.exporter.inverted_tree.catalog import build_catalog
from excel_grapher.exporter.inverted_tree.errors import InvertedTreeExportError
from excel_grapher.series_bindings import load_series_bindings
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from tests.conftest import SyntheticConfiguredPipeline


def test_synthetic_bindings_have_unique_inverted_tree_cell_ownership(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    """build_catalog is the inverted-tree unique-address index (excel-grapher #600)."""
    config = synthetic_configured_pipeline.config
    catalog = build_catalog(
        load_series_bindings(config.bindings_path), workbook=config.workbook_path
    )
    assert catalog.address_to_id


def test_overlapping_bindings_fail_unique_inverted_tree_ownership(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    config = synthetic_configured_pipeline.config
    bindings = load_series_bindings(config.bindings_path)
    series = list(bindings["series"])
    duplicate = dict(series[0])
    duplicate["id"] = f"{duplicate['id']}_duplicate"
    overlapping = cast(
        WorkbookSeriesBindings,
        {**dict(bindings), "series": [*series, duplicate]},
    )
    with pytest.raises(InvertedTreeExportError):
        build_catalog(overlapping, workbook=config.workbook_path)
