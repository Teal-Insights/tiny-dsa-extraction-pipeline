"""Tests for the precomputed series-graph starter layout (``assets/graph/layout.json``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.package_materialize import DIST_OVERLAY_REL
from src.series_graph_layout import (
    SERIES_GRAPH_LAYOUT_REL,
    SeriesGraphTopology,
    compute_series_graph_layout,
)
from tests.test_dist_overlay import _materialize, _write_overlay
from tests.test_package_materialize import _prepare_repo

# Two inputs feed one internal series, which feeds two outputs.
_TOPOLOGY = SeriesGraphTopology(
    node_ids=("a", "b", "mid", "out1", "out2"),
    roles=("input", "input", "internal", "output", "output"),
    edges=(("a", "mid"), ("b", "mid"), ("mid", "out1"), ("mid", "out2")),
)

_OVERLAY_SCHEMA = """\
NODES = (
    {"id": "a", "role": "input"},
    {"id": "b", "role": "input"},
    {"id": "mid", "role": "internal"},
    {"id": "out1", "role": "output"},
    {"id": "out2", "role": "output"},
)
EDGES = (("a", "mid"), ("b", "mid"), ("mid", "out1"), ("mid", "out2"))
"""


def _mean_x(positions: dict[str, tuple[float, float]], ids: tuple[str, ...]) -> float:
    return sum(positions[i][0] for i in ids) / len(ids)


def test_layout_places_every_node() -> None:
    positions = compute_series_graph_layout(_TOPOLOGY)

    assert set(positions) == set(_TOPOLOGY.node_ids)


def test_layout_flows_left_to_right_from_inputs_to_outputs() -> None:
    positions = compute_series_graph_layout(_TOPOLOGY)

    inputs = _mean_x(positions, ("a", "b"))
    internal = _mean_x(positions, ("mid",))
    outputs = _mean_x(positions, ("out1", "out2"))
    assert inputs < internal < outputs


def test_layout_is_deterministic() -> None:
    assert compute_series_graph_layout(_TOPOLOGY) == compute_series_graph_layout(
        _TOPOLOGY
    )


def test_layout_rejects_edges_to_unknown_series() -> None:
    topology = SeriesGraphTopology(
        node_ids=("a",), roles=("input",), edges=(("a", "missing"),)
    )

    with pytest.raises(ValueError, match="missing"):
        compute_series_graph_layout(topology)


def test_materialize_writes_layout_from_overlay_graph_schema(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, "my_model/graph_schema.py", _OVERLAY_SCHEMA)
    _materialize(config)

    payload = json.loads(
        (config.dist_root / SERIES_GRAPH_LAYOUT_REL).read_text(encoding="utf-8")
    )
    assert payload["version"] == 1
    assert payload["orientation"] == "left_to_right"
    assert set(payload["positions"]) == set(_TOPOLOGY.node_ids)
    assert payload["positions"] == {
        series_id: list(xy)
        for series_id, xy in compute_series_graph_layout(_TOPOLOGY).items()
    }


def test_materialize_writes_no_layout_for_empty_scaffold_schema(
    tmp_path: Path,
) -> None:
    config = _prepare_repo(tmp_path)
    _materialize(config)

    assert not (config.dist_root / SERIES_GRAPH_LAYOUT_REL).exists()


def test_materialize_leaves_no_bytecode_in_dist(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, "my_model/graph_schema.py", _OVERLAY_SCHEMA)
    _materialize(config)

    assert not list(config.dist_root.rglob("__pycache__"))


def test_materialize_fails_loudly_when_graph_schema_does_not_import(
    tmp_path: Path,
) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, "my_model/graph_schema.py", "raise ImportError('boom')\n")

    with pytest.raises(RuntimeError, match="boom"):
        _materialize(config)


def test_overlay_may_not_supply_layout(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, SERIES_GRAPH_LAYOUT_REL.as_posix(), "{}\n")

    with pytest.raises(ValueError, match="layout.json"):
        _materialize(config)
    assert (tmp_path / DIST_OVERLAY_REL / SERIES_GRAPH_LAYOUT_REL).is_file()
