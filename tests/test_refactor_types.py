import pytest

from src.refactor_types import (
    clustering_mode_choices,
    parse_clustering_mode,
    parse_variation_mode,
    variation_mode_choices,
)


def test_variation_mode_choices_match_literal() -> None:
    assert variation_mode_choices() == ("independent", "dominant_key_only")


def test_clustering_mode_choices_match_literal() -> None:
    assert clustering_mode_choices() == ("series", "series_ast", "ast")


def test_parse_variation_mode_accepts_valid_values() -> None:
    assert parse_variation_mode("independent") == "independent"
    assert parse_variation_mode("dominant_key_only") == "dominant_key_only"


def test_parse_clustering_mode_accepts_valid_values() -> None:
    assert parse_clustering_mode("series") == "series"
    assert parse_clustering_mode("series_ast") == "series_ast"
    assert parse_clustering_mode("ast") == "ast"


def test_parse_variation_mode_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="VARIATION_MODE"):
        parse_variation_mode("all_keys")


def test_parse_clustering_mode_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="CLUSTERING_MODE"):
        parse_clustering_mode("all_series")
