"""Tests for the viewer's ``force-layout.js``, which fits ``layout.json`` to node boxes."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FORCE_LAYOUT_JS = REPO_ROOT / "templates/series-graph/assets/graph/force-layout.js"

_LINK = 40.0
_GAP = {"gapX": 40.0, "gapY": 18.0}
# Crowded: at the base scale most neighbours overlap.
_POSITIONS = {
    "a": [-40.0, -20.0],
    "b": [-40.0, 20.0],
    "mid": [0.0, 0.0],
    "out1": [40.0, -15.0],
    "out2": [40.0, 15.0],
    "twin": [0.0, 0.0],
}
_SIZES = {
    "a": {"width": 168.0, "height": 64.0},
    "b": {"width": 200.0, "height": 64.0},
    "mid": {"width": 300.0, "height": 64.0},
    "out1": {"width": 360.0, "height": 64.0},
    "out2": {"width": 168.0, "height": 64.0},
    "twin": {"width": 168.0, "height": 64.0},
}


def _place(positions: dict[str, Any], sizes: dict[str, Any]) -> Any:
    node = shutil.which("node")
    assert node is not None, "node is required to test the graph viewer JS"
    script = (
        f"const L = require({json.dumps(str(FORCE_LAYOUT_JS))});"
        "const o = JSON.parse(process.argv[1]);"
        "process.stdout.write(JSON.stringify(L.placeNodes(o)));"
    )
    options = {
        "positions": positions,
        "sizes": sizes,
        "linkDistance": _LINK,
        **_GAP,
    }
    result = subprocess.run(
        [node, "-e", script, json.dumps(options)],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout)


def _slack(
    placed: dict[str, dict[str, float]], sizes: dict[str, Any], a: str, b: str
) -> float:
    """Clearance between two boxes along their better-separated axis."""
    dx = abs(placed[a]["x"] - placed[b]["x"])
    dy = abs(placed[a]["y"] - placed[b]["y"])
    need_x = (sizes[a]["width"] + sizes[b]["width"]) / 2 + _GAP["gapX"]
    need_y = (sizes[a]["height"] + sizes[b]["height"]) / 2 + _GAP["gapY"]
    return max(dx - need_x, dy - need_y)


def _base_scale(sizes: dict[str, Any]) -> float:
    """One link distance maps to the mean box footprint (with gaps)."""
    width = sum(s["width"] for s in sizes.values()) / len(sizes) + _GAP["gapX"]
    height = sum(s["height"] for s in sizes.values()) / len(sizes) + _GAP["gapY"]
    return math.sqrt(width * height) / _LINK


def test_placed_boxes_do_not_overlap() -> None:
    placed = _place(_POSITIONS, _SIZES)

    ids = sorted(_POSITIONS)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            assert _slack(placed, _SIZES, a, b) >= -1e-6, (a, b)


def test_separated_layout_is_only_scaled() -> None:
    positions = {
        "left": [-100.0, 0.0],
        "right": [100.0, 0.0],
        "top": [0.0, -100.0],
    }
    sizes = {series_id: {"width": 200.0, "height": 64.0} for series_id in positions}

    placed = _place(positions, sizes)

    scale = _base_scale(sizes)
    for series_id, (x, y) in positions.items():
        assert placed[series_id]["x"] == pytest.approx(scale * x)
        assert placed[series_id]["y"] == pytest.approx(scale * y)


def test_separation_moves_boxes_only_vertically() -> None:
    """x is the input -> output flow; separating along it could reorder it."""
    placed = _place(_POSITIONS, _SIZES)

    scale = _base_scale(_SIZES)
    for series_id, (x, _) in _POSITIONS.items():
        assert placed[series_id]["x"] == pytest.approx(scale * x)


def test_placement_is_deterministic() -> None:
    assert _place(_POSITIONS, _SIZES) == _place(_POSITIONS, _SIZES)


def test_placement_is_null_when_layout_misses_a_series() -> None:
    sizes = {**_SIZES, "new": {"width": 168.0, "height": 64.0}}

    assert _place(_POSITIONS, sizes) is None
