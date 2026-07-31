"""Unit tests for cluster fingerprint summary and ref-relation inference."""

from __future__ import annotations

from pathlib import Path

from src.internals_refactor import MemberContext
from src.refactor_fingerprints import (
    ClusterFingerprintSummary,
    RefRelation,
    RefResolution,
    SemanticDependencyRef,
    build_cluster_fingerprint_summary,
    classify_ref_relation,
    estimate_fingerprint_dump_tokens,
    estimate_legacy_dump_tokens,
    format_cluster_fingerprint_dump,
    _format_ref_relation_lines,
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
    assert relation.lookup_bases == {"TIME_PERIOD": "TIME_PERIOD"}
    assert relation.lookup_keys == {"TIME_PERIOD": "REF_AREA"}


def test_classify_tuple_lookup_for_jointly_determined_ref_key() -> None:
    """A ref key jointly determined by two member dims becomes a tuple lookup.

    No single member dimension yields a single-valued table (TIME_PERIOD 1 maps
    to both 1991_nominal and 1991_real; INDICATOR nominal_gdp maps to both
    1991_nominal and 1992_nominal), but the (INDICATOR, TIME_PERIOD) pair
    routes every member unambiguously.
    """
    member_keys = {
        "Sheet!B10": {"TIME_PERIOD": 1, "INDICATOR": "nominal_gdp"},
        "Sheet!B11": {"TIME_PERIOD": 1, "INDICATOR": "real_gdp"},
        "Sheet!C10": {"TIME_PERIOD": 2, "INDICATOR": "nominal_gdp"},
        "Sheet!C11": {"TIME_PERIOD": 2, "INDICATOR": "real_gdp"},
    }
    ref_keys_by_member = {
        "Sheet!B10": {"INDICATOR_YEAR": "1991_nominal"},
        "Sheet!B11": {"INDICATOR_YEAR": "1991_real"},
        "Sheet!C10": {"INDICATOR_YEAR": "1992_nominal"},
        "Sheet!C11": {"INDICATOR_YEAR": "1992_real"},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "lookup"
    assert relation.lookup_keys == {"INDICATOR_YEAR": ("INDICATOR", "TIME_PERIOD")}
    assert relation.lookups == {
        "INDICATOR_YEAR": {
            ("nominal_gdp", 1): "1991_nominal",
            ("real_gdp", 1): "1991_real",
            ("nominal_gdp", 2): "1992_nominal",
            ("real_gdp", 2): "1992_real",
        }
    }
    assert relation.lookup_bases == {}
    assert relation.explicit is None


def test_dump_renders_tuple_lookup_table() -> None:
    members = (
        _member("Data!B10", "=Hist!B2"),
        _member("Data!B11", "=Hist!B3"),
        _member("Data!C10", "=Hist!C2"),
        _member("Data!C11", "=Hist!C3"),
    )
    bound_keys = {
        "Data!B10": {"INDICATOR": "nominal_gdp", "TIME_PERIOD": 1},
        "Data!B11": {"INDICATOR": "real_gdp", "TIME_PERIOD": 1},
        "Data!C10": {"INDICATOR": "nominal_gdp", "TIME_PERIOD": 2},
        "Data!C11": {"INDICATOR": "real_gdp", "TIME_PERIOD": 2},
        "Hist!B2": {"INDICATOR_YEAR": "1991_nominal"},
        "Hist!B3": {"INDICATOR_YEAR": "1991_real"},
        "Hist!C2": {"INDICATOR_YEAR": "1992_nominal"},
        "Hist!C3": {"INDICATOR_YEAR": "1992_real"},
    }
    expected = {
        address: bound_keys[address]
        for address in ("Data!B10", "Data!B11", "Data!C10", "Data!C11")
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
    )
    assert summary.fallback_reason is None
    assert summary.relation_tiers == ("lookup",)
    dump = format_cluster_fingerprint_dump(summary)
    assert "INDICATOR_YEAR = table[(INDICATOR, TIME_PERIOD)]" in dump
    assert "(nominal_gdp, 1): 1991_nominal" in dump


def test_classify_same_dimension_lookup_for_cross_reads() -> None:
    """Cross-reads along the members' own dimension derive a same-dim lookup.

    The strict single-dim search rejects the table because it merely restates a
    unique-per-member key; the subset fallback accepts it since the recorded
    routing is ground truth for synthesis.
    """
    member_keys = {
        "Sheet!B10": {"INDICATOR": "debt"},
        "Sheet!B11": {"INDICATOR": "revenue"},
        "Sheet!B12": {"INDICATOR": "expenditure"},
    }
    ref_keys_by_member = {
        "Sheet!B10": {"INDICATOR": "revenue"},
        "Sheet!B11": {"INDICATOR": "gdp"},
        "Sheet!B12": {"INDICATOR": "gdp"},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "lookup"
    assert relation.lookup_keys == {"INDICATOR": "INDICATOR"}
    assert relation.lookups == {
        "INDICATOR": {"debt": "revenue", "revenue": "gdp", "expenditure": "gdp"}
    }
    assert relation.explicit is None


def test_classify_irregular_routing_derives_smallest_single_valued_subset() -> None:
    """The previously-explicit irregular fixture now derives scalar lookups.

    TIME_PERIOD is unique per member, so single-dim tables keyed by it are
    single-valued but non-compressing; the strict search rejects them and the
    subset fallback accepts them (smallest subset first, so no tuple needed).
    """
    member_keys = {
        "Sheet!B10": {"TIME_PERIOD": 1, "REF_AREA": "USA"},
        "Sheet!C10": {"TIME_PERIOD": 2, "REF_AREA": "USA"},
        "Sheet!D10": {"TIME_PERIOD": 3, "REF_AREA": "FRA"},
    }
    ref_keys_by_member = {
        "Sheet!B10": {"TIME_PERIOD": 9, "REF_AREA": "JPN"},
        "Sheet!C10": {"TIME_PERIOD": 1, "REF_AREA": "CAN"},
        "Sheet!D10": {"TIME_PERIOD": 7, "REF_AREA": "MEX"},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "lookup"
    assert relation.lookup_keys == {
        "REF_AREA": "TIME_PERIOD",
        "TIME_PERIOD": "TIME_PERIOD",
    }
    assert relation.lookups["REF_AREA"] == {1: "JPN", 2: "CAN", 3: "MEX"}
    assert relation.lookups["TIME_PERIOD"] == {1: 9, 2: 1, 3: 7}
    assert relation.explicit is None


def test_classify_irregular_falls_back_to_explicit() -> None:
    # Two members share the same key combo but route to different refs, so no
    # table over member dims — not even the full tuple — is single-valued.
    member_keys = {
        "Sheet!B10": {"TIME_PERIOD": 1},
        "Sheet!C10": {"TIME_PERIOD": 1},
    }
    ref_keys_by_member = {
        "Sheet!B10": {"REF_AREA": "JPN"},
        "Sheet!C10": {"REF_AREA": "CAN"},
    }
    relation = classify_ref_relation(0, member_keys, ref_keys_by_member)
    assert relation.tier == "explicit"
    assert relation.explicit is not None
    assert len(relation.explicit) == 2
    first_member, first_ref = relation.explicit[0]
    assert ("TIME_PERIOD", 1) in first_member
    assert ("REF_AREA", "JPN") in first_ref


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
    ref_keys = dict(group.ref_keys_by_member)
    assert set(ref_keys) == set(group.members)
    assert ref_keys["Data!E20"][0]["TIME_PERIOD"] == 4
    assert ref_keys["Data!G20"][0]["TIME_PERIOD"] == 6


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


def test_format_ref_relation_col_by_includes_numeric_indices() -> None:
    """Geometry hints must expose 1-based indices matching xl_index_ref tuples."""
    relation = RefRelation(
        ref_index=0,
        tier="constant",
        series_id=None,
        fixed_keys={},
        identity_dims=(),
        offsets={},
        lookups={},
        explicit=None,
        resolution=RefResolution(
            kind="xl_cell",
            sheet="Climate Database",
            address_template="'Climate Database'!{col}26",
            col_by_dim=(
                (
                    "TIME_PERIOD",
                    ((2029, "Q"), (2030, "R"), (2039, "AA"), (2090, "BZ")),
                ),
            ),
        ),
    )
    text = "\n".join(_format_ref_relation_lines(relation))
    assert "col by TIME_PERIOD" in text
    assert "2029: Q=17" in text
    assert "2030: R=18" in text
    assert "2039: AA=27" in text
    assert "2090: BZ=78" in text
    assert "2029: Q," not in text
    assert "2029: Q}" not in text


def test_build_summary_splits_groups_when_ref_slot_series_mix() -> None:
    """Members sharing a skeleton but landing in different ref series are split.

    Mirrors cluster 236 / ``interest_rate_long_run_real_interest_rate``: one
    formula shape, three ``ref_1`` operand regimes. Without a split the dump
    emits one mixed ``table[TIME_PERIOD]`` and no ``series`` / ``reads`` line.
    """
    members = (
        _member(
            "Rate!B19",
            "=(1+Anchor!B5/100)*(1+Macro!AE15/100)*100-100",
        ),
        _member(
            "Rate!C19",
            "=(1+Anchor!B5/100)*(1+Inflation!B9/100)*100-100",
        ),
        _member(
            "Rate!D19",
            "=(1+Anchor!B5/100)*(1+Inflation!BC3/100)*100-100",
        ),
    )
    bound_keys = {
        "Rate!B19": {"TIME_PERIOD": 2002},
        "Rate!C19": {"TIME_PERIOD": 2003},
        "Rate!D19": {"TIME_PERIOD": 2028},
        "Anchor!B5": {},
        "Macro!AE15": {"TIME_PERIOD": 2029},
        "Inflation!B9": {"TIME_PERIOD": 2002},
        "Inflation!BC3": {"TIME_PERIOD": 2055},
    }
    expected = {
        "Rate!B19": {"TIME_PERIOD": 2002},
        "Rate!C19": {"TIME_PERIOD": 2003},
        "Rate!D19": {"TIME_PERIOD": 2028},
    }
    address_to_series_id = {
        "Anchor!B5": "anchor_series",
        "Macro!AE15": "macrofiscal_gdp_deflator_growth",
        "Inflation!B9": "inflation_convergence_trajectory",
        "Inflation!BC3": "inflation_path",
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id=address_to_series_id,
    )
    assert summary.fallback_reason is None
    assert len(summary.groups) == 3
    members_by_group = {group.members: group for group in summary.groups}
    assert set(members_by_group) == {
        ("Rate!B19",),
        ("Rate!C19",),
        ("Rate!D19",),
    }
    assert (
        members_by_group[("Rate!B19",)].ref_relations[1].series_id
        == "macrofiscal_gdp_deflator_growth"
    )
    assert (
        members_by_group[("Rate!C19",)].ref_relations[1].series_id
        == "inflation_convergence_trajectory"
    )
    assert (
        members_by_group[("Rate!D19",)].ref_relations[1].series_id == "inflation_path"
    )
    for group in summary.groups:
        ref1 = group.ref_relations[1]
        assert ref1.series_id is not None
        # No group may keep a helper-oriented table that spans regimes.
        period_lookup = ref1.lookups.get("TIME_PERIOD", {})
        assert not ({2002, 2028} <= set(period_lookup))

    dump = format_cluster_fingerprint_dump(summary)
    assert "2002: 2029" not in dump
    assert "2028: 2055" not in dump
    assert "table[TIME_PERIOD]" not in dump
    assert "series macrofiscal_gdp_deflator_growth" in dump
    assert "series inflation_convergence_trajectory" in dump
    assert "series inflation_path" in dump


def test_build_summary_keeps_uniform_ref_series_together() -> None:
    """Same skeleton + same per-slot series stays one fingerprint group."""
    members = (
        _member("Rate!C19", "=(1+Anchor!B5/100)*(1+Inflation!B9/100)*100-100"),
        _member("Rate!D19", "=(1+Anchor!B5/100)*(1+Inflation!C9/100)*100-100"),
    )
    bound_keys = {
        "Rate!C19": {"TIME_PERIOD": 2003},
        "Rate!D19": {"TIME_PERIOD": 2004},
        "Anchor!B5": {},
        "Inflation!B9": {"TIME_PERIOD": 2002},
        "Inflation!C9": {"TIME_PERIOD": 2003},
    }
    expected = {
        "Rate!C19": {"TIME_PERIOD": 2003},
        "Rate!D19": {"TIME_PERIOD": 2004},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id={
            "Anchor!B5": "anchor_series",
            "Inflation!B9": "inflation_convergence_trajectory",
            "Inflation!C9": "inflation_convergence_trajectory",
        },
    )
    assert summary.fallback_reason is None
    assert len(summary.groups) == 1
    assert summary.groups[0].members == ("Rate!C19", "Rate!D19")
    assert (
        summary.groups[0].ref_relations[1].series_id
        == "inflation_convergence_trajectory"
    )
    assert summary.groups[0].ref_relations[1].tier == "offset"
    assert summary.groups[0].ref_relations[1].offsets == {"TIME_PERIOD": -1}


def test_build_summary_splits_groups_when_ref_slot_helper_mix() -> None:
    """Same series published as two helpers must split into helper-uniform groups.

    Reproduces the live cluster-28 failure: a passthrough sweep (``=Baseline!D33``
    ...) whose operand series ``baseline_interest_rate`` was peeled by the
    scheduler into two helpers (``baseline_interest_rate`` for early years,
    ``baseline_interest_rate_2`` for later years). ``address_to_series_id`` reports
    one series for every operand, so keying the partition on series alone keeps
    them in one group whose slot spans two helpers — ``_resolve_ref`` then can't
    pick one helper (every member ref must be ⊆ one dependency) and falls back to
    ``xl_cell``, which mechanical synthesis rejects as ``slots_without_read_sites``.
    Partitioning by the resolved helper splits the group so each slot resolves.
    """
    members = (
        _member("HotAdapted!D32", "=Baseline!D33"),
        _member("HotAdapted!E32", "=Baseline!E33"),
        _member("HotAdapted!F32", "=Baseline!F33"),
        _member("HotAdapted!G32", "=Baseline!G33"),
    )
    bound_keys = {
        "HotAdapted!D32": {"TIME_PERIOD": 2009},
        "HotAdapted!E32": {"TIME_PERIOD": 2010},
        "HotAdapted!F32": {"TIME_PERIOD": 2030},
        "HotAdapted!G32": {"TIME_PERIOD": 2031},
        "Baseline!D33": {"TIME_PERIOD": 2009},
        "Baseline!E33": {"TIME_PERIOD": 2010},
        "Baseline!F33": {"TIME_PERIOD": 2030},
        "Baseline!G33": {"TIME_PERIOD": 2031},
    }
    expected = {
        "HotAdapted!D32": {"TIME_PERIOD": 2009},
        "HotAdapted!E32": {"TIME_PERIOD": 2010},
        "HotAdapted!F32": {"TIME_PERIOD": 2030},
        "HotAdapted!G32": {"TIME_PERIOD": 2031},
    }
    # One binding series, but two published helpers (scheduler peel).
    address_to_series_id = {
        "Baseline!D33": "baseline_interest_rate",
        "Baseline!E33": "baseline_interest_rate",
        "Baseline!F33": "baseline_interest_rate",
        "Baseline!G33": "baseline_interest_rate",
    }
    semantic_dependencies = (
        SemanticDependencyRef(
            helper_name="baseline_interest_rate",
            call_form="baseline_interest_rate(ctx, time_period=time_period)",
            address_template="Baseline!{col}33",
            addresses=("Baseline!D33", "Baseline!E33"),
        ),
        SemanticDependencyRef(
            helper_name="baseline_interest_rate_2",
            call_form="baseline_interest_rate_2(ctx, time_period=time_period)",
            address_template="Baseline!{col}33",
            addresses=("Baseline!F33", "Baseline!G33"),
        ),
    )
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id=address_to_series_id,
        semantic_dependencies=semantic_dependencies,
    )
    assert summary.fallback_reason is None
    assert len(summary.groups) == 2
    helper_by_members = {
        group.members: group.ref_relations[0].resolution for group in summary.groups
    }
    assert set(helper_by_members) == {
        ("HotAdapted!D32", "HotAdapted!E32"),
        ("HotAdapted!F32", "HotAdapted!G32"),
    }
    early = helper_by_members[("HotAdapted!D32", "HotAdapted!E32")]
    late = helper_by_members[("HotAdapted!F32", "HotAdapted!G32")]
    assert early.kind == "semantic_helper"
    assert early.helper_name == "baseline_interest_rate"
    assert late.kind == "semantic_helper"
    assert late.helper_name == "baseline_interest_rate_2"
    # Both groups still carry the single binding series id.
    for group in summary.groups:
        assert group.ref_relations[0].series_id == "baseline_interest_rate"


def test_build_summary_falls_back_when_unbound_refs_mix_sheet_row() -> None:
    """Incomplete series maps: unbound mates with mixed sheet/row → fallback.

    Members whose operands are all unbound share regime ``(None,)`` and would
    otherwise stay one fingerprint group even when they land on different
    sheets / latent series.
    """
    members = (
        _member("Result!B1", "=X!A1"),
        _member("Result!C1", "=W!A1"),
        _member("Result!D1", "=Y!A1"),
    )
    bound_keys = {
        "Result!B1": {"TIME_PERIOD": 2002},
        "Result!C1": {"TIME_PERIOD": 2003},
        "Result!D1": {"TIME_PERIOD": 2004},
        "X!A1": {"TIME_PERIOD": 2002},
        "W!A1": {"TIME_PERIOD": 2003},
        "Y!A1": {"TIME_PERIOD": 2004},
    }
    expected = {
        "Result!B1": {"TIME_PERIOD": 2002},
        "Result!C1": {"TIME_PERIOD": 2003},
        "Result!D1": {"TIME_PERIOD": 2004},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        # Only Y is bound; X and W share regime (None,) after series partition.
        address_to_series_id={"Y!A1": "ya"},
    )
    assert summary.fallback_reason is not None
    assert "unbound_ref_slot_geometry_conflict" in summary.fallback_reason
    assert summary.groups == ()


def test_build_summary_keeps_same_geometry_unbound_refs_together() -> None:
    """Unbound column-sweep mates that share sheet/row stay one group."""
    members = (
        _member("Result!B1", "=X!A1"),
        _member("Result!C1", "=X!B1"),
    )
    bound_keys = {
        "Result!B1": {"TIME_PERIOD": 2002},
        "Result!C1": {"TIME_PERIOD": 2003},
        "X!A1": {"TIME_PERIOD": 2002},
        "X!B1": {"TIME_PERIOD": 2003},
    }
    expected = {
        "Result!B1": {"TIME_PERIOD": 2002},
        "Result!C1": {"TIME_PERIOD": 2003},
    }
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        # Partial map: neither operand bound, but geometry agrees on (X, 1).
        address_to_series_id={
            "Result!B1": "result_series",
            "Result!C1": "result_series",
        },
    )
    assert summary.fallback_reason is None
    assert len(summary.groups) == 1
    assert summary.groups[0].members == ("Result!B1", "Result!C1")
    assert summary.groups[0].ref_relations[0].series_id is None


def test_build_summary_splits_peel_boundary_lag_from_self_recurrence() -> None:
    """A lag that crosses the peel boundary must not share a group with self-lags.

    Issue #138: an engine series peeled into two schedule units publishes two
    helpers (``paris_engine_indicators`` for the early years,
    ``paris_engine_indicators_2`` for the rest). Inside the later unit the first
    member's ``t-1`` operand lives in the *sibling* helper while every other
    member's lag is in-cluster self-recurrence. Both regimes key on the same
    binding series id, so partitioning on the series alone kept them in one
    group whose slot is neither wholly in-cluster nor wholly inside one
    dependency — ``_resolve_ref`` fell back to ``xl_cell``, leaving a raw
    ``xl_cell(ctx, 'Paris!{col}35')`` read in the generated helper.
    """
    members = (
        _member("Paris!E35", "=Paris!D35*(1+Paris!E32/100)"),
        _member("Paris!F35", "=Paris!E35*(1+Paris!F32/100)"),
        _member("Paris!G35", "=Paris!F35*(1+Paris!G32/100)"),
    )
    bound_keys = {
        "Paris!D35": {"TIME_PERIOD": 2029},
        "Paris!E35": {"TIME_PERIOD": 2030},
        "Paris!F35": {"TIME_PERIOD": 2031},
        "Paris!G35": {"TIME_PERIOD": 2032},
        "Paris!E32": {"TIME_PERIOD": 2030},
        "Paris!F32": {"TIME_PERIOD": 2031},
        "Paris!G32": {"TIME_PERIOD": 2032},
    }
    expected = {
        "Paris!E35": {"TIME_PERIOD": 2030},
        "Paris!F35": {"TIME_PERIOD": 2031},
        "Paris!G35": {"TIME_PERIOD": 2032},
    }
    # One binding series for every row-35 cell, peeled into two schedule units.
    address_to_series_id = {
        "Paris!D35": "paris_engine_indicators",
        "Paris!E35": "paris_engine_indicators",
        "Paris!F35": "paris_engine_indicators",
        "Paris!G35": "paris_engine_indicators",
        "Paris!E32": "paris_engine_weighted_interest_rate",
        "Paris!F32": "paris_engine_weighted_interest_rate",
        "Paris!G32": "paris_engine_weighted_interest_rate",
    }
    semantic_dependencies = (
        SemanticDependencyRef(
            helper_name="paris_engine_indicators",
            call_form="paris_engine_indicators(ctx, time_period=time_period)",
            address_template="Paris!{col}35",
            addresses=("Paris!D35",),
        ),
        SemanticDependencyRef(
            helper_name="paris_engine_weighted_interest_rate",
            call_form=(
                "paris_engine_weighted_interest_rate(ctx, time_period=time_period)"
            ),
            address_template="Paris!{col}32",
            addresses=("Paris!E32", "Paris!F32", "Paris!G32"),
        ),
    )
    summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected,
        bound_address_keys=bound_keys,
        workbook_path=None,
        layout=None,
        address_to_series_id=address_to_series_id,
        semantic_dependencies=semantic_dependencies,
    )
    assert summary.fallback_reason is None
    resolutions = {
        group.members: group.ref_relations[0].resolution for group in summary.groups
    }
    assert set(resolutions) == {("Paris!E35",), ("Paris!F35", "Paris!G35")}
    boundary = resolutions[("Paris!E35",)]
    assert boundary.kind == "semantic_helper"
    assert boundary.helper_name == "paris_engine_indicators"
    assert resolutions[("Paris!F35", "Paris!G35")].kind == "self_recurrence"
    # The interest-rate slot stays one helper for every member.
    for group in summary.groups:
        assert group.ref_relations[1].resolution.kind == "semantic_helper"
        assert (
            group.ref_relations[1].resolution.helper_name
            == "paris_engine_weighted_interest_rate"
        )
