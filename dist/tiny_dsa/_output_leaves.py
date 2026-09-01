from __future__ import annotations

# --- Series binding output leaf tables ---

Record = dict[str, object]

_OUTPUT_LEAVES_OUTPUT_BASELINE: list[tuple[str, Record]] = [
    (
        "Outputs!B12",
        {"SCENARIO": "baseline", "TIME_PERIOD": 1, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!C12",
        {"SCENARIO": "baseline", "TIME_PERIOD": 2, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!D12",
        {"SCENARIO": "baseline", "TIME_PERIOD": 3, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!E12",
        {"SCENARIO": "baseline", "TIME_PERIOD": 4, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!F12",
        {"SCENARIO": "baseline", "TIME_PERIOD": 5, "UNIT_MEASURE": "PC_GDP"},
    ),
]

_OUTPUT_LEAVES_OUTPUT_SHOCKED: list[tuple[str, Record]] = [
    (
        "Outputs!B13",
        {"SCENARIO": "shocked", "TIME_PERIOD": 1, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!C13",
        {"SCENARIO": "shocked", "TIME_PERIOD": 2, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!D13",
        {"SCENARIO": "shocked", "TIME_PERIOD": 3, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!E13",
        {"SCENARIO": "shocked", "TIME_PERIOD": 4, "UNIT_MEASURE": "PC_GDP"},
    ),
    (
        "Outputs!F13",
        {"SCENARIO": "shocked", "TIME_PERIOD": 5, "UNIT_MEASURE": "PC_GDP"},
    ),
]

_OUTPUT_LEAVES_OUTPUT_DELTA: list[tuple[str, Record]] = [
    (
        "Outputs!B14",
        {
            "SCENARIO": "shocked_minus_baseline",
            "TIME_PERIOD": 1,
            "UNIT_MEASURE": "PP",
        },
    ),
    (
        "Outputs!C14",
        {
            "SCENARIO": "shocked_minus_baseline",
            "TIME_PERIOD": 2,
            "UNIT_MEASURE": "PP",
        },
    ),
    (
        "Outputs!D14",
        {
            "SCENARIO": "shocked_minus_baseline",
            "TIME_PERIOD": 3,
            "UNIT_MEASURE": "PP",
        },
    ),
    (
        "Outputs!E14",
        {
            "SCENARIO": "shocked_minus_baseline",
            "TIME_PERIOD": 4,
            "UNIT_MEASURE": "PP",
        },
    ),
    (
        "Outputs!F14",
        {
            "SCENARIO": "shocked_minus_baseline",
            "TIME_PERIOD": 5,
            "UNIT_MEASURE": "PP",
        },
    ),
]
