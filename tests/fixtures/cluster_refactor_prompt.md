You will be provided mechanical Python translations of a cluster of Excel formula cells. Your task is to refactor them into a single domain-aware parameterized Python function.

## Output format

Return only JSON matching the response schema:

```json
{
  "additionalProperties": false,
  "properties": {
    "symbol_signature": {
      "description": "Python function signature, including `def` keyword, `snake_case` semantic name, `ctx: EvalContext`, typed economic parameters from `key_vocabulary`, and return type hint.",
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
      "description": "One entry per cluster member with a unique combination of binding key values for triangulating that address.",
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
    },
    "uses_first_year_branch": {
      "description": "True when the helper branches on first-year logic (e.g. time_period == 1 or prior-period recursion).",
      "type": "boolean"
    }
  },
  "required": [
    "symbol_signature",
    "symbol_docstring",
    "symbol_body",
    "parameters",
    "member_keys",
    "uses_first_year_branch"
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
- Return type should be documented with a type hint, e.g. `-> float`.
- Series-constant binding keys (`scope: series`) are not parameters; bake them into the helper.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- Document each economic parameter in `Args`, not binding concept names.
- You may omit Python string delimiters.

## Body

- Emit `symbol_body` for one self-contained function; no nested helpers or imports.
- Call only runtime symbols from the member translations and, if necessary, Python stdlib functions/operators.
- Preserve dependency function names and signatures.
- For every semantic dependency, use the provided `call_form` with pass-through parameter names, e.g. `prior_period_total(ctx, reporting_period=reporting_period)`.
- Map economic parameters to workbook columns internally when reading `xl_cell` addresses; prefer a lookup table, not an `if`/`elif` ladder.
- Keep `xl_eval` only for leaf inputs read with `xl_cell`; never for refactored cells.
- Do not reference `cell_*` helpers anywhere in the body.
- Rename local temporaries to domain-meaningful `snake_case` informed by naming hints.

## Parameters

- Declare `parameters[]` using binding key concepts from `key_vocabulary`; do not use column letters.
- `parameters[].concept` must use binding concept names (e.g. `TIME_PERIOD`), not parameter names.
- `parameters[].name` must match `suggested_param_name` from `key_vocabulary`.

## Member keys

- Emit one `member_keys[]` entry per cluster member.
- Copy `keys[]` from the provided `expected_keys` for each member.
- `member_keys[].keys[].concept` must use binding concept names (e.g. `TIME_PERIOD`), not parameter names.

## Example

Refactor will mostly consist of generalizing parallel cell functions into one helper and unpacking nested calls for readability. For example, suppose you are assigned to refactor a cluster covering `Forecast!B12:F12` with canonical template `=IF(Forecast!{col}4>=Assumptions!$C$2,1,0)` and `REPORTING_PERIOD` as the only varying binding key. Each member currently reads its period column from a hard-coded address.

In this case, you could map `reporting_period` to workbook columns with a lookup table, assign `observed_value` and `threshold` from `xl_cell`, and return `1.0` or `0.0` from a readable comparison. With `table_labels` containing "Quarterly Forecast" and `row_labels` containing "Growth Threshold Met", you might name the helper `growth_threshold_met`.

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
  ],
  "uses_first_year_branch": false
}
```

To support function naming, docstring generation, and parameterization, you will be provided cluster member sources, `key_vocabulary`, `expected_keys` per member, semantic dependency `call_form` strings, and label metadata. Use `expected_keys` verbatim for `member_keys` and `key_vocabulary` for `parameters`.
