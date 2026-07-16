"""Unit tests for cluster fingerprint summary and ref-relation inference."""

from __future__ import annotations

from pathlib import Path

from src.internals_refactor import MemberContext
from src.refactor_fingerprints import (
    ClusterFingerprintSummary,
    RefRelation,
    build_cluster_fingerprint_summary,
    classify_ref_relation,
    estimate_fingerprint_dump_tokens,
    estimate_legacy_dump_tokens,
    format_cluster_fingerprint_dump,
)
from src.workbook_addresses import ProjectionColumnLayout


def _member(
    address: str,
    formula: str,
    *,
    engine_column: str | None = None,
) -> MemberContext:
    sheet, colrow = address.split("!", 1)
    column = "".join(c for c in colrow if c.isalpha())
    row = int("".join(c for c in colrow if c.isdigit()))
    resolved_column = engine_column or column
    function_name = f"cell_{sheet.lower()}_{column.lower()}{row}"
    return MemberContext(
        address=address,
        function_name=function_name,
        engine_column=resolved_column,
        normalized_formula=formula,
        python_source=(
            f"def {function_name}(ctx):\n    return xl_cell(ctx, '{address}')\n"
        ),
        dependency_addresses=(),
        dependency_functions=(),
    )


def test_classify_constant_ref() -> None:
    member_keys = {
        "Sheet!B10": {"TIME_PERIOD": 1, "REF_AREA": "USA"},
        "Sheet!C10": {"TIME_PERIOD": 2, "REF_AREA": "USA"},
        "Sheet!D10": {"TIME_PERIOD": 3, "REF_AREA": "USA"},
    }
    ref_keys_by_member = {
        "Sheet!B10": {"THRESHOLD": 0.5},
        "Sheet!C10": {"THRESHOLD": 0.5},
        "Sheet!D10": {"THRESHOLD": 0.5},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "constant"
    assert relation.fixed_keys == {"THRESHOLD": 0.5}
    assert relation.identity_dims == ()
    assert relation.offsets == {}
    assert relation.lookups == {}
    assert relation.explicit is None


def test_classify_identity_and_offset_ref() -> None:
    member_keys = {
        "Sheet!C10": {"TIME_PERIOD": 2, "REF_AREA": "USA"},
        "Sheet!D10": {"TIME_PERIOD": 3, "REF_AREA": "FRA"},
        "Sheet!E10": {"TIME_PERIOD": 4, "REF_AREA": "USA"},
    }
    ref_keys_by_member = {
        "Sheet!C10": {"TIME_PERIOD": 1, "REF_AREA": "USA"},
        "Sheet!D10": {"TIME_PERIOD": 2, "REF_AREA": "FRA"},
        "Sheet!E10": {"TIME_PERIOD": 3, "REF_AREA": "USA"},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "offset"
    assert relation.identity_dims == ("REF_AREA",)
    assert relation.offsets == {"TIME_PERIOD": -1}
    assert relation.lookups == {}
    assert relation.explicit is None


def test_classify_uniform_identity_sweep() -> None:
    member_keys = {
        "Sheet!B10": {"TIME_PERIOD": 1},
        "Sheet!C10": {"TIME_PERIOD": 2},
        "Sheet!D10": {"TIME_PERIOD": 3},
    }
    ref_keys_by_member = {
        "Sheet!B10": {"TIME_PERIOD": 1},
        "Sheet!C10": {"TIME_PERIOD": 2},
        "Sheet!D10": {"TIME_PERIOD": 3},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "identity"
    assert relation.identity_dims == ("TIME_PERIOD",)
    assert relation.offsets == {}


def test_classify_ragged_lag_lookup_by_row_dim() -> None:
    """Ragged lags keyed by REF_AREA use the lookup tier.

    For each member, ref.TIME_PERIOD == member.TIME_PERIOD - lag[member.REF_AREA]
    with lag {USA: 1, FRA: 3}. The stored lookup maps member.REF_AREA -> ref.TIME_PERIOD
    only when that relation alone is single-valued; with a sweeping TIME_PERIOD that
    is not single-valued, classification instead records a lag-by-dim table under
    ``lookups`` keyed by the dim whose lag varies (REF_AREA → lag), with identity
    on REF_AREA and member.TIME_PERIOD as the base. The implementation exposes the
    lag table via ``lookups['TIME_PERIOD']`` holding {REF_AREA: lag_delta}.
    """
    member_keys = {
        "Sheet!E20": {"REF_AREA": "USA", "TIME_PERIOD": 4},
        "Sheet!F20": {"REF_AREA": "USA", "TIME_PERIOD": 5},
        "Sheet!E24": {"REF_AREA": "FRA", "TIME_PERIOD": 4},
        "Sheet!F24": {"REF_AREA": "FRA", "TIME_PERIOD": 5},
    }
    ref_keys_by_member = {
        "Sheet!E20": {"REF_AREA": "USA", "TIME_PERIOD": 3},
        "Sheet!F20": {"REF_AREA": "USA", "TIME_PERIOD": 4},
        "Sheet!E24": {"REF_AREA": "FRA", "TIME_PERIOD": 1},
        "Sheet!F24": {"REF_AREA": "FRA", "TIME_PERIOD": 2},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "lookup"
    assert relation.identity_dims == ("REF_AREA",)
    assert relation.lookups == {"TIME_PERIOD": {"USA": 1, "FRA": 3}}


def test_classify_irregular_falls_back_to_explicit() -> None:
    member_keys = {
        "Sheet!B10": {"TIME_PERIOD": 1, "REF_AREA": "USA"},
        "Sheet!C10": {"TIME_PERIOD": 2, "REF_AREA": "USA"},
        "Sheet!D10": {"TIME_PERIOD": 3, "REF_AREA": "FRA"},
    }
    # No consistent relation (neither offset nor single-dim lookup).
    ref_keys_by_member = {
        "Sheet!B10": {"TIME_PERIOD": 9, "REF_AREA": "JPN"},
        "Sheet!C10": {"TIME_PERIOD": 1, "REF_AREA": "CAN"},
        "Sheet!D10": {"TIME_PERIOD": 7, "REF_AREA": "MEX"},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "explicit"
    assert relation.explicit is not None
    assert len(relation.explicit) == 3
    first_member, first_ref = relation.explicit[0]
    assert ("REF_AREA", "USA") in first_member
    assert ("TIME_PERIOD", 1) in first_member


def test_build_summary_uniform_sweep_single_group() -> None:
    members = (
        _member("Data!E20", "=Data!E4", engine_column="E"),
        _member("Data!F20", "=Data!F4", engine_column="F"),
        _member("Data!G20", "=Data!G4", engine_column="G"),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4, "REF_AREA": "USA"},
        "Data!F20": {"TIME_PERIOD": 5, "REF_AREA": "USA"},
        "Data!G20": {"TIME_PERIOD": 6, "REF_AREA": "USA"},
        "Data!E4": {"TIME_PERIOD": 4, "REF_AREA": "USA"},
        "Data!F4": {"TIME_PERIOD": 5, "REF_AREA": "USA"},
        "Data!G4": {"TIME_PERIOD": 6, "REF_AREA": "USA"},
    }
    expected_member_keys = {
        address: {"TIME_PERIOD": bound_keys[address]["TIME_PERIOD"]}
        for address in ("Data!E20", "Data!F20", "Data!G20")
    }
    layout = ProjectionColumnLayout(
        engine_sheet="Data",
        engine_columns=("E", "F", "G"),
        outputs_sheet="Outputs",
        outputs_column_to_engine={},
        time_period_to_engine_column={4: "E", 5: "F", 6: "G"},
    )
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected_member_keys,
        bound_address_keys=bound_keys,
        workbook_path=Path("/tmp/unused.xlsx"),
        layout=layout,
        address_to_series_id={
            "Data!E4": "DEBT_STOCK",
            "Data!F4": "DEBT_STOCK",
            "Data!G4": "DEBT_STOCK",
        },
    )
    assert isinstance(summary, ClusterFingerprintSummary)
    assert summary.fallback_reason is None
    assert len(summary.groups) == 1
    group = summary.groups[0]
    assert group.skeleton_text.startswith("=ref_0")
    assert group.members == ("Data!E20", "Data!F20", "Data!G20")
    assert group.exemplar.address == "Data!E20"
    assert len(group.ref_relations) == 1
    assert group.ref_relations[0].tier == "identity"
    assert group.ref_relations[0].series_id == "DEBT_STOCK"
    assert summary.key_space == {"TIME_PERIOD": (4, 5, 6)}
    assert summary.key_to_column == {4: "E", 5: "F", 6: "G"}


def test_build_summary_missing_ref_keys_falls_back() -> None:
    members = (
        _member("Data!E20", "=Data!E4"),
        _member("Data!F20", "=Data!F4"),
    )
    bound_keys = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
        # refs intentionally unbound
    }
    expected = {
        "Data!E20": {"TIME_PERIOD": 4},
        "Data!F20": {"TIME_PERIOD": 5},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is not None
    assert (
        "missing" in summary.fallback_reason.lower()
        or "unbound" in summary.fallback_reason.lower()
    )


def test_build_summary_multi_dim_and_dump_mentions_all_members() -> None:
    members = (
        _member("Data!E20", "=Data!E4-Data!D4"),
        _member("Data!F20", "=Data!F4-Data!E4"),
        _member("Data!E28", "=Data!E12-Data!D12"),
        _member("Data!F28", "=Data!F12-Data!E12"),
    )
    bound_keys = {
        "Data!E20": {"REF_AREA": "USA", "TIME_PERIOD": 4},
        "Data!F20": {"REF_AREA": "USA", "TIME_PERIOD": 5},
        "Data!E28": {"REF_AREA": "FRA", "TIME_PERIOD": 4},
        "Data!F28": {"REF_AREA": "FRA", "TIME_PERIOD": 5},
        "Data!E4": {"REF_AREA": "USA", "TIME_PERIOD": 4},
        "Data!F4": {"REF_AREA": "USA", "TIME_PERIOD": 5},
        "Data!D4": {"REF_AREA": "USA", "TIME_PERIOD": 3},
        "Data!E12": {"REF_AREA": "FRA", "TIME_PERIOD": 4},
        "Data!F12": {"REF_AREA": "FRA", "TIME_PERIOD": 5},
        "Data!D12": {"REF_AREA": "FRA", "TIME_PERIOD": 3},
    }
    expected = {
        address: {
            "REF_AREA": bound_keys[address]["REF_AREA"],
            "TIME_PERIOD": bound_keys[address]["TIME_PERIOD"],
        }
        for address in ("Data!E20", "Data!F20", "Data!E28", "Data!F28")
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is None
    assert len(summary.groups) == 1
    assert len(summary.groups[0].ref_relations) == 2
    assert summary.groups[0].ref_relations[0].tier == "identity"
    assert summary.groups[0].ref_relations[1].tier == "offset"
    dump = format_cluster_fingerprint_dump(summary)
    assert "4 of 4 members" in dump
    assert "REF_AREA" in dump
    assert "TIME_PERIOD" in dump
    assert "Exemplar translation" in dump
    assert "cell_data_e20" in dump


def test_token_estimates_fingerprint_smaller_than_legacy_for_large_cluster() -> None:
    members = tuple(_member(f"Data!B{20 + i}", f"=Data!B{4 + i}") for i in range(40))
    bound_keys: dict[str, dict[str, int]] = {}
    expected: dict[str, dict[str, int]] = {}
    for i, member in enumerate(members):
        period = 1 + i
        bound_keys[member.address] = {"TIME_PERIOD": period}
        bound_keys[f"Data!B{4 + i}"] = {"TIME_PERIOD": period}
        expected[member.address] = {"TIME_PERIOD": period}
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is None
    legacy = estimate_legacy_dump_tokens(members, member_limit=30)
    fingerprint = estimate_fingerprint_dump_tokens(summary)
    assert fingerprint < legacy
    assert isinstance(summary.groups[0].ref_relations[0], RefRelation)
