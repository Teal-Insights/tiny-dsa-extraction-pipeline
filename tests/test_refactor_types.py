import pytest

from src.refactor_types import parse_variation_mode, variation_mode_choices


def test_variation_mode_choices_match_literal() -> None:
    assert variation_mode_choices() == ("independent", "dominant_key_only")


def test_parse_variation_mode_accepts_valid_values() -> None:
    assert parse_variation_mode("independent") == "independent"
    assert parse_variation_mode("dominant_key_only") == "dominant_key_only"


def test_parse_variation_mode_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="VARIATION_MODE"):
        parse_variation_mode("all_keys")
