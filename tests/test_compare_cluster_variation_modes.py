"""CLI contract for scripts.compare_cluster_variation_modes."""

from __future__ import annotations

import pytest

from scripts.compare_cluster_variation_modes import parse_args


def test_parse_args_defaults_clustering_mode_to_none() -> None:
    args = parse_args([])
    assert args.clustering_mode is None
    assert args.no_cache is False
    assert args.include == []


def test_parse_args_accepts_clustering_mode_override() -> None:
    args = parse_args(["--clustering-mode", "series_ast", "--include", "fingerprints"])
    assert args.clustering_mode == "series_ast"
    assert args.include == ["fingerprints"]


def test_parse_args_rejects_invalid_clustering_mode() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--clustering-mode", "all_series"])
