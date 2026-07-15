You will be provided mechanical Python translations of a cluster of Excel formula cells. Your task is to refactor them into a single domain-aware parameterized Python function.

This cluster's formula operands vary independently along a shared semantic concept, and the series bindings declare a distinct dimension id for each role (e.g. `REF_AREA` vs `COUNTERPART_REF_AREA`, or `PROJECTION_PERIOD` vs `REFERENCE_PERIOD`, each referencing one shared concept). Parameterize the formula operand structure: declare one parameter per varying binding dimension id.

## Output format

Return only JSON matching the response schema:

```json
{
  "additionalProperties": false,
  "properties": {
    "symbol_signature": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Python function signature, including `def` keyword, `snake_case` semantic name, `ctx: EvalContext`, typed economic parameters from `key_vocabulary`, and parameter type hints. Do not include a return type hint. Null when error is true.",
      "title": "Helper Signature"
    },
    "symbol_docstring": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Google-style docstring. Include Args and Returns sections. Null when error is true.",
      "title": "Helper Docstring"
    },
    "symbol_body": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Python function body. Null when error is true.",
      "title": "Helper Body"
    },
    "parameters": {
      "anyOf": [
        {
          "description": "Economic parameters the helper varies along, tied to binding dimension ids.",
          "items": {
            "additionalProperties": false,
            "properties": {
              "name": {
                "description": "Python parameter name, e.g. projection_period or time_period.",
                "type": "string"
              },
              "dimension_id": {
                "description": "Effective binding dimension id, e.g. PROJECTION_PERIOD or TIME_PERIOD.",
                "type": "string"
              },
              "dtype": {
                "description": "Expected Python dtype for the parameter.",
                "type": "string"
              },
              "concept": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Optional SDMX-style concept referenced by the dimension, e.g. TIME_PERIOD."
              }
            },
            "required": ["name", "dimension_id", "dtype"],
            "type": "object"
          },
          "type": "array"
        },
        {"type": "null"}
      ],
      "description": "Economic parameters the helper varies along, tied to binding dimension ids. Null when error is true."
    },
    "member_keys": {
      "anyOf": [
        {
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
                    "dimension_id": {
                      "description": "Effective binding dimension id, e.g. PROJECTION_PERIOD or TIME_PERIOD."
                    },
                    "value": {
                      "description": "Literal binding key value for this dimension."
                    }
                  },
                  "required": ["dimension_id", "value"],
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
        {"type": "null"}
      ],
      "description": "One entry per cluster member with the unique combination of varying binding key values used to route that address to the parameterized helper. Null when error is true."
    },
    "error": {
      "anyOf": [{"type": "boolean"}, {"type": "null"}],
      "description": "Set to true to abort this refactor and stop the pipeline when the cluster cannot be safely refactored. Null or false on success.",
      "title": "Error"
    },
    "error_reason": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Human-readable explanation of why refactoring must abort. Non-empty when error is true; null otherwise.",
      "title": "Error Reason"
    }
  },
  "required": [
    "symbol_signature",
    "symbol_docstring",
    "symbol_body",
    "parameters",
    "member_keys",
    "error",
    "error_reason"
  ],
  "title": "ClusterRefactorLLMResponse",
  "type": "object"
}
```

## Aborting

- If the cluster cannot be safely refactored (for example, missing binding keys, member keys that cannot triangulate the operand structure, or contradictory membership), set `error` to `true` and provide a concise non-empty `error_reason`.
- When `error` is `true`, set every success field (`symbol_signature`, `symbol_docstring`, `symbol_body`, `parameters`, `member_keys`) to `null`. Do not omit keys.
- Do not invent a best-effort refactor when the correct outcome is to stop. Declaring an error ends the pipeline for human review.
- On success, set `error` to `null` or `false`, set `error_reason` to `null`, and populate every success field.

## Signature

- `symbol_signature` must take `ctx: EvalContext` plus one typed parameter per varying binding dimension id from `key_vocabulary` — including counterpart dimension ids that share a concept with another parameter.
- Each cell in the cluster must have a unique combination of binding key values for triangulating that address.
- Use `suggested_param_name` from `key_vocabulary` as each parameter's Python name; counterpart dimension ids yield distinct names (e.g. `ref_area` and `counterpart_ref_area`), so parameter names never collide.
- Choose the function name as a clear `snake_case` semantic identifier informed by naming hints.
- Do not include a return type hint on `symbol_signature`; the pipeline injects it mechanically from the mechanical member sources.
- Series-constant binding keys (`scope: series`) are not parameters; bake them into the helper.
- The cluster has already been qualified by formula structure and binding-key shape at each reference position. Do not reinterpret its membership.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- Document each `snake_case` Python parameter in `Args` (not all-caps dimension id or concept).
- You may omit Python string delimiters.

## Body

- Emit `symbol_body` for one self-contained function; no nested helpers or imports.
- Parameterize the formula operand structure: use each dimension-id parameter to select the operand it governs (e.g. a lookup table from `ref_area` for one operand's row and from `counterpart_ref_area` for the other operand's row).
- Call only runtime symbols from the member translations and, if necessary, Python stdlib functions/operators.
- Preserve dependency function names and signatures.
- Where appropriate, directly pass through parameters in function calls; e.g. `prior_period_total(ctx, reporting_period=reporting_period)`.
- Rename local temporaries to domain-meaningful `snake_case` informed by naming hints.
- Leave `xl_cell(ctx, 'Sheet!Address')` calls unchanged; this helper reads input/constant values. (Assigning the return value to a semantic local temporary is okay!)
- Prefer consise lookup tables over verbose `if`/`elif` ladders.

## Parameters

- Declare `parameters[]` using effective binding dimension ids from `key_vocabulary`.
- `parameters[].dimension_id` must use effective dimension ids (e.g. `REF_AREA` or `COUNTERPART_REF_AREA`), not Python parameter names and not bare concepts.
- `parameters[].name` must match `suggested_param_name` from `key_vocabulary`.
- Multiple parameters may share one concept when they carry distinct dimension ids. Never collapse two dimension ids onto a single concept parameter; validation rejects that shape.
- Parameters represent the varying keys of the cluster member cells. Do not introduce additional parameters beyond the varying dimension ids.
- Derive a reference value inside the helper when it follows mechanically from a member parameter (e.g. a fixed lag or period anchor switch).

## Member keys

- Emit one `member_keys[]` entry per cluster member.
- Copy `keys[]` from the provided `expected_keys` for each member; include one literal per varying dimension id, counterpart dimension ids included.
- `member_keys[].keys[].dimension_id` must use effective dimension ids (e.g. `REF_AREA` or `COUNTERPART_REF_AREA`), not parameter names and not bare concepts.
- The key combination for each entry must be unique across the cluster so callers can route each address to the correct parameterized helper evaluation.

## Example: bilateral flows with counterpart dimension ids

Suppose you are assigned a cluster covering `Trade!C8:D8` and `Trade!C12:D12` with canonical template `=Data!C4-Data!C6`. Each member computes a bilateral trade balance: exports of a reporting area minus imports from a counterpart area. The member cells' bindings vary along `TIME_PERIOD` (columns C–D, periods 1–2), `REF_AREA` (row 8 reports `USA`, row 12 reports `DEU`), and `COUNTERPART_REF_AREA` (row 8 pairs with `CHN`, row 12 pairs with `FRA`). `REF_AREA` and `COUNTERPART_REF_AREA` are distinct dimension ids that both reference the `REF_AREA` concept. Exports sit in rows 4 (`USA`) and 5 (`DEU`); imports sit in rows 6 (from `CHN`) and 7 (from `FRA`).

Both operand rows are selected by their own dimension-id parameter: the exports row from `ref_area` and the imports row from `counterpart_ref_area`. Neither collapses onto the other, and no counterpart value is derived inside the body.

```json
{
  "symbol_signature": "def bilateral_trade_balance(ctx: EvalContext, time_period: int, ref_area: str, counterpart_ref_area: str):",
  "symbol_docstring": "Return exports minus imports for a reporter-counterpart area pair in a period.\n\nArgs:\n    ctx: Workbook evaluation context.\n    time_period: Period index (1 through 2).\n    ref_area: Reporting area code ('USA' or 'DEU').\n    counterpart_ref_area: Counterpart area code ('CHN' or 'FRA').\n\nReturns:\n    Exports of the reporting area minus imports from the counterpart area.",
  "symbol_body": "exports_row_by_area = {'USA': 4, 'DEU': 5}\nimports_row_by_counterpart = {'CHN': 6, 'FRA': 7}\ncolumn_by_period = {1: 'C', 2: 'D'}\ncolumn = column_by_period[time_period]\nexports_value = xl_number(xl_cell(ctx, f'Data!{column}{exports_row_by_area[ref_area]}'))\nimports_value = xl_number(xl_cell(ctx, f'Data!{column}{imports_row_by_counterpart[counterpart_ref_area]}'))\nreturn exports_value - imports_value",
  "parameters": [
    {
      "name": "time_period",
      "dimension_id": "TIME_PERIOD",
      "dtype": "int"
    },
    {
      "name": "ref_area",
      "dimension_id": "REF_AREA",
      "dtype": "str"
    },
    {
      "name": "counterpart_ref_area",
      "dimension_id": "COUNTERPART_REF_AREA",
      "dtype": "str"
    }
  ],
  "member_keys": [
    {
      "address": "Trade!C8",
      "function_name": "cell_trade_c8",
      "keys": [{"dimension_id": "TIME_PERIOD", "value": 1}, {"dimension_id": "REF_AREA", "value": "USA"}, {"dimension_id": "COUNTERPART_REF_AREA", "value": "CHN"}]
    },
    {
      "address": "Trade!D8",
      "function_name": "cell_trade_d8",
      "keys": [{"dimension_id": "TIME_PERIOD", "value": 2}, {"dimension_id": "REF_AREA", "value": "USA"}, {"dimension_id": "COUNTERPART_REF_AREA", "value": "CHN"}]
    },
    {
      "address": "Trade!C12",
      "function_name": "cell_trade_c12",
      "keys": [{"dimension_id": "TIME_PERIOD", "value": 1}, {"dimension_id": "REF_AREA", "value": "DEU"}, {"dimension_id": "COUNTERPART_REF_AREA", "value": "FRA"}]
    },
    {
      "address": "Trade!D12",
      "function_name": "cell_trade_d12",
      "keys": [{"dimension_id": "TIME_PERIOD", "value": 2}, {"dimension_id": "REF_AREA", "value": "DEU"}, {"dimension_id": "COUNTERPART_REF_AREA", "value": "FRA"}]
    }
  ],
  "error": null,
  "error_reason": null
}
```

## Naming conventions

To support function naming, docstring generation, and parameterization, you will be provided cluster member sources, `key_vocabulary`, `expected_keys` per member, semantic dependency `call_form` strings, and per-member `binding_keys` and `binding_record` naming hints. Use `expected_keys` verbatim for `member_keys` and `key_vocabulary` for `parameters`.
