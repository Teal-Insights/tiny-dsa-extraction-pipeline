from excel_grapher.grapher import DependencyGraph
from excel_grapher.grapher.label_detection import (
    BehaviorRule,
    LabelDetectionBehavior,
    LabelDetectionConfig,
    LabelDetectionContext,
    LabelResult,
    RegionLabelParams,
    RegionSelector,
    region_specs_from_ranges,
)


class OffsetYearAndRowLabelScan(LabelDetectionBehavior):
    """Collect labels from configured header rows and row labels from the left."""

    name = "offset_year_and_row_label_scan"

    def detect(self, ctx: LabelDetectionContext) -> LabelResult:
        ws = ctx.ws_values
        if ws is None:
            return LabelResult()

        column_labels: list[str] = []
        header_rows = (
            ctx.region_params.header_rows if ctx.region_params is not None else ()
        )
        if ctx.row not in header_rows:
            for header_row in header_rows:
                value = ws.cell(row=header_row, column=ctx.col).value
                if isinstance(value, str):
                    text = value.strip()
                    if text:
                        column_labels.append(text)
                elif isinstance(value, int) and not isinstance(value, bool):
                    column_labels.append(str(value))
                elif isinstance(value, float) and value.is_integer():
                    column_labels.append(str(int(value)))

        row_labels: list[str] = []
        scan_col = ctx.col - 1
        while scan_col >= 1:
            value = ws.cell(row=ctx.row, column=scan_col).value
            if isinstance(value, str):
                text = value.strip()
                if text:
                    row_labels.append(text)
                    break
            scan_col -= 1

        return LabelResult(
            row_labels=tuple(row_labels), column_labels=tuple(column_labels)
        )


def build_label_detection_config() -> LabelDetectionConfig:
    """Build label detection configuration for Tiny DSA extraction."""

    def selector(*ranges: str) -> RegionSelector:
        return RegionSelector(include=region_specs_from_ranges(ranges))

    rules = (
        # Country/profile/shock table regions in Inputs
        BehaviorRule(
            name="inputs-country-selector",
            selector=selector("Inputs!$A$5:$B$6"),
            behaviors=("left_edge_scan",),
            stop_after_match=True,
        ),
        BehaviorRule(
            name="inputs-country-profile",
            selector=selector("Inputs!$A$10:$B$12"),
            behaviors=(
                "left_edge_scan",
                "right_edge_scan",
                "offset_year_and_row_label_scan",
            ),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(9,)),
        ),
        BehaviorRule(
            name="inputs-baseline-vectors",
            selector=selector("Inputs!$C$16:$G$18"),
            behaviors=("offset_year_and_row_label_scan",),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(15,)),
        ),
        BehaviorRule(
            name="inputs-shock-config",
            selector=selector("Inputs!$B$21:$B$22"),
            behaviors=("left_edge_scan",),
            stop_after_match=True,
        ),
        BehaviorRule(
            name="inputs-shock-table",
            selector=selector("Inputs!$B$26:$D$26"),
            behaviors=("left_edge_scan", "offset_year_and_row_label_scan"),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(25,)),
        ),
        # Engine table regions
        BehaviorRule(
            name="engine-baseline-path",
            selector=selector("Engine!$B$5:$G$6"),
            behaviors=("offset_year_and_row_label_scan",),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(5,)),
        ),
        BehaviorRule(
            name="engine-shock-activation-cell",
            selector=selector("Engine!$B$9"),
            behaviors=("left_edge_scan",),
            stop_after_match=True,
        ),
        BehaviorRule(
            name="engine-shock-activation-row",
            selector=selector("Engine!$C$10:$G$10"),
            behaviors=("offset_year_and_row_label_scan",),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(5,)),
        ),
        BehaviorRule(
            name="engine-shocked-parameters",
            selector=selector("Engine!$C$14:$G$16"),
            behaviors=("offset_year_and_row_label_scan",),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(13,)),
        ),
        BehaviorRule(
            name="engine-shocked-path",
            selector=selector("Engine!$C$20:$G$20"),
            behaviors=("offset_year_and_row_label_scan",),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(19,)),
        ),
        # Output table region
        BehaviorRule(
            name="outputs-trajectory",
            selector=selector("Outputs!$B$12:$F$14"),
            behaviors=("offset_year_and_row_label_scan",),
            stop_after_match=True,
            region_params=RegionLabelParams(header_rows=(11,)),
        ),
    )

    return LabelDetectionConfig(
        enabled=True,
        fallback_behaviors=("left_edge_scan",),
        rules=rules,
    )


LABEL_DETECTION_CONFIG = build_label_detection_config()
LABEL_BEHAVIORS: tuple[LabelDetectionBehavior, ...] = (OffsetYearAndRowLabelScan(),)
TABLE_LABEL_REGIONS: tuple[tuple[str, str], ...] = (
    ("COUNTRY SELECTOR", "Inputs!B5:B6"),
    ("COUNTRY PROFILE TABLE (lookup data)", "Inputs!A10:B12"),
    ("BASELINE PARAMETERS", "Inputs!C16:G18"),
    ("SHOCK CONFIGURATION", "Inputs!B21:B22"),
    ("SHOCK TABLE (lookup by shock type, in percentage points)", "Inputs!B26:D26"),
    ("BASELINE PATH", "Engine!B5:G6"),
    ("SHOCK ACTIVATION", "Engine!B9:B9"),
    ("SHOCK ACTIVATION", "Engine!C10:G10"),
    ("SHOCKED PARAMETERS", "Engine!C14:G16"),
    ("SHOCKED PATH", "Engine!C20:G20"),
    ("DEBT-TO-GDP TRAJECTORY", "Outputs!B12:F14"),
)


def _annotate_table_labels(graph_obj: DependencyGraph) -> None:
    """Add table-level labels from section headers to node metadata."""

    def col_to_index(col: str) -> int:
        value = 0
        for ch in col:
            value = value * 26 + (ord(ch) - ord("A") + 1)
        return value

    def index_to_col(index: int) -> str:
        chars: list[str] = []
        while index > 0:
            index, rem = divmod(index - 1, 26)
            chars.append(chr(ord("A") + rem))
        return "".join(reversed(chars))

    for table_label, sheet_range in TABLE_LABEL_REGIONS:
        sheet, a1_range = sheet_range.split("!", maxsplit=1)
        left, right = a1_range.split(":", maxsplit=1)
        left_col = "".join(ch for ch in left if ch.isalpha())
        left_row = int("".join(ch for ch in left if ch.isdigit()))
        right_col = "".join(ch for ch in right if ch.isalpha())
        right_row = int("".join(ch for ch in right if ch.isdigit()))

        min_col = col_to_index(left_col)
        max_col = col_to_index(right_col)
        for row in range(left_row, right_row + 1):
            for col in range(min_col, max_col + 1):
                key = f"{sheet}!{index_to_col(col)}{row}"
                node = graph_obj.get_node(key)
                if node is None:
                    continue
                metadata = dict(node.metadata)
                table_labels = list(metadata.get("table_labels", []))
                if table_label not in table_labels:
                    table_labels.append(table_label)
                metadata["table_labels"] = table_labels
                graph_obj.set_node_metadata(key, metadata)
