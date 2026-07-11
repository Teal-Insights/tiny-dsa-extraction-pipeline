You will be provided mechanical Python translations of a cluster of Excel formula cells. Your task is to refactor them into a single domain-aware parameterized Python function.

## Output format

Return only JSON matching the response schema:

```json
{
  "additionalProperties": false,
  "properties": {
    "symbol_signature": {
      "description": "Python function signature, including `def` keyword, `snake_case` semantic name, `ctx: EvalContext`, typed economic parameters from `key_vocabulary`, and a scalar return type hint: `bool`, `float`, `int`, `str`, or a `|` union of those types.",
      "title": "Helper Signature",
      "type": "string"
    },
    "symbol_docstring": {
      "description": "Google-style docstring. Include Args and Returns sections.",
      "title": "Helper Docstring",
      "type": "string"
    },
    "symbol_body": {
      "description": "Python function body.",
      "title": "Helper Body",
      "type": "string"
    },
    "parameters": {
      "description": "Economic parameters the helper varies along, tied to binding key concepts.",
      "items": {
        "additionalProperties": false,
        "properties": {
          "name": {
            "description": "Python parameter name, e.g. time_period.",
            "type": "string"
          },
          "concept": {
            "description": "Binding key concept, e.g. TIME_PERIOD.",
            "type": "string"
          },
          "dtype": {
            "description": "Expected Python dtype for the parameter.",
            "type": "string"
          }
        },
        "required": ["name", "concept", "dtype"],
        "type": "object"
      },
      "type": "array"
    },
    "member_keys": {
      "description": "One entry per cluster member with the unique combination of varying binding key values used to route that address to the parameterized helper.",
      "items": {
        "additionalProperties": false,
        "properties": {
          "address": {
            "description": "Workbook address this entry covers.",
            "type": "string"
          },
          "function_name": {
            "description": "Existing cell_* function being replaced.",
            "type": "string"
          },
          "keys": {
            "items": {
              "additionalProperties": false,
              "properties": {
                "concept": {
                  "description": "Binding key concept name, e.g. TIME_PERIOD.",
                  "type": "string"
                },
                "value": {
                  "description": "Literal binding key value for this concept."
                }
              },
              "required": ["concept", "value"],
              "type": "object"
            },
            "type": "array"
          }
        },
        "required": ["address", "function_name", "keys"],
        "type": "object"
      },
      "type": "array"
    }
  },
  "required": [
    "symbol_signature",
    "symbol_docstring",
    "symbol_body",
    "parameters",
    "member_keys"
  ],
  "title": "ClusterRefactorLLMResponse",
  "type": "object"
}
```

## Signature

- `symbol_signature` must take `ctx: EvalContext` plus one typed parameter per varying binding concept from `key_vocabulary`.
- Each cell in the cluster must have a unique combination of binding key values for triangulating that address.
- Use `suggested_param_name` from `key_vocabulary` as each parameter's Python name.
- Choose the function name as a clear `snake_case` semantic identifier informed by naming hints.
- Return type must be one of `bool`, `float`, `int`, or `str`, or a `|` union composed only of those types, e.g. `-> float | str`.
- Series-constant binding keys (`scope: series`) are not parameters; bake them into the helper.
- The cluster has already been qualified by formula structure and binding-key shape at each reference position. Do not reinterpret its membership or add parameters for individual reference positions.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- Document each economic parameter in `Args`, not binding concept names.
- You may omit Python string delimiters.

## Body

- Emit `symbol_body` for one self-contained function; no nested helpers or imports.
- Call only runtime symbols from the member translations and, if necessary, Python stdlib functions/operators.
- Preserve dependency function names and signatures.
- Where appropriate, directly pass through parameters in function calls; e.g. `prior_period_total(ctx, reporting_period=reporting_period)`.
- Rename local temporaries to domain-meaningful `snake_case` informed by naming hints.
- Leave `xl_cell(ctx, 'Sheet!Address')` calls unchanged; this helper reads input/constant values. (Assigning the return value to a semantic local temporary is okay!)
- Prefer consise lookup tables over verbose `if`/`elif` ladders.

## Parameters

- Declare `parameters[]` using binding key concepts from `key_vocabulary`; do not use column letters.
- `parameters[].concept` must use binding concept names (e.g. `TIME_PERIOD`), not parameter names.
- `parameters[].name` must match `suggested_param_name` from `key_vocabulary`.
- Parameters represent varying keys of the cluster member cells. Do not introduce additional parameters such as `counterpart_ref_area` or `reference_time_period` solely because multiple operands use the same concept.
- Derive a reference period inside the helper when it follows from a member parameter. Arbitrary independently varying operand values are not supported by this response contract.

## Member keys

- Emit one `member_keys[]` entry per cluster member.
- Copy `keys[]` from the provided `expected_keys` for each member.
- `member_keys[].keys[].concept` must use binding concept names (e.g. `TIME_PERIOD`), not parameter names.
- The key combination for each entry must be unique across the cluster so callers can route each address to the correct parameterized helper evaluation.

## Example 1: Unpacking nested calls

Refactor will mostly consist of generalizing parallel cell functions into one helper and unpacking nested calls for readability. For example, suppose you are assigned to refactor a cluster covering `Forecast!B12:F12` with canonical template `=IF(Forecast!{col}4>=Assumptions!$C$2,1,0)` and `REPORTING_PERIOD` as the only varying binding key. Each member currently reads its period column from a hard-coded address.

In this case, you could map `reporting_period` to workbook columns with a lookup table, assign `observed_value` and `threshold` from `xl_cell`, and return `1.0` or `0.0` from a readable comparison. With each member's `binding_record` carrying `TABLE: Quarterly Forecast` and `INDICATOR: growth_threshold_met`, you might name the helper `growth_threshold_met`.

```json
{
  "symbol_signature": "def growth_threshold_met(ctx: EvalContext, reporting_period: int) -> float:",
  "symbol_docstring": "Return 1.0 when the observed value meets or exceeds the growth threshold for the reporting period.\n\nArgs:\n    ctx: Workbook evaluation context.\n    reporting_period: Reporting period index (1 through 5).\n\nReturns:\n    1.0 if the observed value is at or above the threshold, else 0.0.",
  "symbol_body": "column_by_period = {1: 'B', 2: 'C', 3: 'D', 4: 'E', 5: 'F'}\ncolumn = column_by_period[reporting_period]\nobserved_value = xl_cell(ctx, f'Forecast!{column}4')\nthreshold = xl_cell(ctx, 'Assumptions!C2')\nmeets_threshold = xl_compare('>=', observed_value, threshold)\nreturn 1.0 if meets_threshold else 0.0",
  "parameters": [
    {
      "name": "reporting_period",
      "concept": "REPORTING_PERIOD",
      "dtype": "int"
    }
  ],
  "member_keys": [
    {
      "address": "Forecast!B12",
      "function_name": "cell_forecast_b12",
      "keys": [{"concept": "REPORTING_PERIOD", "value": 1}]
    },
    {
      "address": "Forecast!C12",
      "function_name": "cell_forecast_c12",
      "keys": [{"concept": "REPORTING_PERIOD", "value": 2}]
    },
    {
      "address": "Forecast!D12",
      "function_name": "cell_forecast_d12",
      "keys": [{"concept": "REPORTING_PERIOD", "value": 3}]
    },
    {
      "address": "Forecast!E12",
      "function_name": "cell_forecast_e12",
      "keys": [{"concept": "REPORTING_PERIOD", "value": 4}]
    },
    {
      "address": "Forecast!F12",
      "function_name": "cell_forecast_f12",
      "keys": [{"concept": "REPORTING_PERIOD", "value": 5}]
    }
  ]
}
```

## Example 2: representing supported lagged reference periods

Some formulas read the same indicator at more than one period, e.g. a change computed as current minus prior period. Both operands vary along `TIME_PERIOD` semantically, but `parameters` and `member_keys` may only carry the cell's own sweep key. Instead of using a second `TIME_PERIOD`-like parameter for the lagged operand, derive the reference period inside the body from `time_period` (e.g. `reference_period = time_period - 1`) and map it to a column with the same lookup table used for the current period.

This example applies only when the reference period can be derived or selected from the member parameters. It does not generalize to arbitrary independent operand values.

When the lag itself differs across row groups, the rows will be keyed by another varying binding concept (e.g. `REF_AREA`); select the lag with a lookup table keyed by that parameter, exactly like any other row-dependent constant.

For example, suppose you are assigned a cluster covering `Data!E20:H20` and `Data!E24:H24`, with varying binding keys `TIME_PERIOD` (columns E–H, periods 4–7) and `REF_AREA` (row 20 is `USA`, row 24 is `FRA`). Source values sit in row 4 (`USA`) and row 8 (`FRA`) across columns B–H (periods 1–7). Each `USA` member computes `=E4-D4`-style differences against the previous period (lag 1), while each `FRA` member computes `=E8-B8`-style differences against three periods earlier (lag 3). Neither lag becomes a parameter: both are baked into the body and switched on `ref_area`.

```json
{
  "symbol_signature": "def indicator_change_from_reference_period(ctx: EvalContext, time_period: int, ref_area: str) -> float:",
  "symbol_docstring": "Return the change in the observed indicator relative to its area-specific reference period.\n\nArgs:\n    ctx: Workbook evaluation context.\n    time_period: Period index (4 through 7).\n    ref_area: Reference area code ('USA' or 'FRA').\n\nReturns:\n    Current-period value minus the lagged value (lag 1 for USA, lag 3 for FRA).",
  "symbol_body": "source_row_by_area = {'USA': 4, 'FRA': 8}\nlag_by_area = {'USA': 1, 'FRA': 3}\ncolumn_by_period = {1: 'B', 2: 'C', 3: 'D', 4: 'E', 5: 'F', 6: 'G', 7: 'H'}\nsource_row = source_row_by_area[ref_area]\ncurrent_value = xl_number(xl_cell(ctx, f'Data!{column_by_period[time_period]}{source_row}'))\nreference_period = time_period - lag_by_area[ref_area]\nreference_value = xl_number(xl_cell(ctx, f'Data!{column_by_period[reference_period]}{source_row}'))\nreturn current_value - reference_value",
  "parameters": [
    {
      "name": "time_period",
      "concept": "TIME_PERIOD",
      "dtype": "int"
    },
    {
      "name": "ref_area",
      "concept": "REF_AREA",
      "dtype": "str"
    }
  ],
  "member_keys": [
    {
      "address": "Data!E20",
      "function_name": "cell_data_e20",
      "keys": [{"concept": "TIME_PERIOD", "value": 4}, {"concept": "REF_AREA", "value": "USA"}]
    },
    {
      "address": "Data!F20",
      "function_name": "cell_data_f20",
      "keys": [{"concept": "TIME_PERIOD", "value": 5}, {"concept": "REF_AREA", "value": "USA"}]
    },
    {
      "address": "Data!G20",
      "function_name": "cell_data_g20",
      "keys": [{"concept": "TIME_PERIOD", "value": 6}, {"concept": "REF_AREA", "value": "USA"}]
    },
    {
      "address": "Data!H20",
      "function_name": "cell_data_h20",
      "keys": [{"concept": "TIME_PERIOD", "value": 7}, {"concept": "REF_AREA", "value": "USA"}]
    },
    {
      "address": "Data!E24",
      "function_name": "cell_data_e24",
      "keys": [{"concept": "TIME_PERIOD", "value": 4}, {"concept": "REF_AREA", "value": "FRA"}]
    },
    {
      "address": "Data!F24",
      "function_name": "cell_data_f24",
      "keys": [{"concept": "TIME_PERIOD", "value": 5}, {"concept": "REF_AREA", "value": "FRA"}]
    },
    {
      "address": "Data!G24",
      "function_name": "cell_data_g24",
      "keys": [{"concept": "TIME_PERIOD", "value": 6}, {"concept": "REF_AREA", "value": "FRA"}]
    },
    {
      "address": "Data!H24",
      "function_name": "cell_data_h24",
      "keys": [{"concept": "TIME_PERIOD", "value": 7}, {"concept": "REF_AREA", "value": "FRA"}]
    }
  ]
}
```

## Naming conventions

To support function naming, docstring generation, and parameterization, you will be provided cluster member sources, `key_vocabulary`, `expected_keys` per member, semantic dependency `call_form` strings, and per-member `binding_keys` and `binding_record` naming hints. Use `expected_keys` verbatim for `member_keys` and `key_vocabulary` for `parameters`.
