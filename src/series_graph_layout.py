"""Precompute the series-graph starter layout for the Cytoscape viewer.

excel-grapher's shared clustered force layout (``clustered_force_layout``)
places the authored series DAG from ``{package}/graph_schema.py``: one cluster
per role, with a weak pull towards input depth inside and between clusters.
The result is rotated so data flows left to right and written to
``dist/assets/graph/layout.json`` in layout units (``link_distance`` is the
spring rest length). The viewer scales it to its node boxes, so the package and
its frontend never import excel-grapher.

``graph_schema.py`` imports the generated package, so topology is read in a
subprocess rooted at ``dist/``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from excel_grapher.grapher.viz_layout import (
    FORCE_LINK_DISTANCE,
    clustered_force_layout,
    input_depths,
)

from src.pipeline_config import PipelineConfig

SERIES_GRAPH_LAYOUT_REL = Path("assets/graph/layout.json")
SERIES_GRAPH_LAYOUT_VERSION = 1
_POSITION_DECIMALS = 2

_READ_TOPOLOGY_SCRIPT = """\
import json, sys
from importlib import import_module

schema = import_module(sys.argv[1] + ".graph_schema")
json.dump(
    {
        "nodes": [[node["id"], node["role"]] for node in schema.NODES],
        "edges": [list(edge) for edge in schema.EDGES],
    },
    sys.stdout,
)
"""


@dataclass(frozen=True, slots=True)
class SeriesGraphTopology:
    """Series ids, their roles, and producer -> consumer edges."""

    node_ids: tuple[str, ...]
    roles: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]


def compute_series_graph_layout(
    topology: SeriesGraphTopology,
) -> dict[str, tuple[float, float]]:
    """Return left-to-right starter positions per series id, in layout units."""
    index = {series_id: i for i, series_id in enumerate(topology.node_ids)}
    unknown = sorted({s for edge in topology.edges for s in edge if s not in index})
    if unknown:
        raise ValueError(f"series-graph edges reference unknown series: {unknown}")
    # input_depths wants (consumer, producer) pairs.
    edges = [(index[target], index[source]) for source, target in topology.edges]
    n = len(topology.node_ids)
    positions = clustered_force_layout(
        n,
        edges,
        topology.roles,
        depths=input_depths(n, edges),
        rank_pull="everywhere",
    )
    # The engine pulls inputs towards the smallest y; the viewer flows along x.
    return {
        series_id: (
            round(float(positions[i, 1]), _POSITION_DECIMALS),
            round(float(positions[i, 0]), _POSITION_DECIMALS),
        )
        for i, series_id in enumerate(topology.node_ids)
    }


def read_series_graph_topology(config: PipelineConfig) -> SeriesGraphTopology:
    """Import ``{package}.graph_schema`` from ``dist/`` and return its topology."""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            _READ_TOPOLOGY_SCRIPT,
            config.dist_metadata.package_name,
        ],
        cwd=config.dist_root,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not read series-graph topology from "
            f"{config.package_root / 'graph_schema.py'}:\n{result.stderr}"
        )
    payload = json.loads(result.stdout)
    return SeriesGraphTopology(
        node_ids=tuple(series_id for series_id, _ in payload["nodes"]),
        roles=tuple(role for _, role in payload["nodes"]),
        edges=tuple((source, target) for source, target in payload["edges"]),
    )


def write_series_graph_layout(config: PipelineConfig) -> None:
    """Write ``assets/graph/layout.json``, or remove it when no series are authored."""
    path = config.dist_root / SERIES_GRAPH_LAYOUT_REL
    topology = read_series_graph_topology(config)
    if not topology.node_ids:
        path.unlink(missing_ok=True)
        return
    payload = {
        "version": SERIES_GRAPH_LAYOUT_VERSION,
        "generator": f"excel-grapher {version('excel-grapher')} clustered_force_layout",
        "orientation": "left_to_right",
        "link_distance": FORCE_LINK_DISTANCE,
        "positions": {
            series_id: list(xy)
            for series_id, xy in compute_series_graph_layout(topology).items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
