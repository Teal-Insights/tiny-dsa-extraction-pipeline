You will be provided a mechanical Python translation of a single-cell Excel formula. Your task is to rename and refactor it as a domain-aware semantic function.

## Output format

Return only JSON matching the response schema:

```json
{
  "additionalProperties": false,
  "properties": {
    "symbol_signature": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Python function signature, including `def` keyword, `snake_case` semantic name, a single `ctx: EvalContext` argument, and a scalar return type hint: `bool`, `float`, `int`, `str`, or a `|` union of those types. Null when error is true.",
      "title": "Symbol Signature"
    },
    "symbol_docstring": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Google-style docstring. Include Args and Returns sections. Null when error is true.",
      "title": "Symbol Docstring"
    },
    "symbol_body": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Python function body. Null when error is true.",
      "title": "Symbol Body"
    },
    "error": {
      "anyOf": [{"type": "boolean"}, {"type": "null"}],
      "description": "Set to true to abort this refactor and stop the pipeline when the cell cannot be safely refactored. Null or false on success.",
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
    "error",
    "error_reason"
  ],
  "title": "SingletonRefactorLLMResponse",
  "type": "object"
}
```

## Aborting

- If the cell cannot be safely refactored, set `error` to `true` and provide a concise non-empty `error_reason`.
- When `error` is `true`, set every success field (`symbol_signature`, `symbol_docstring`, `symbol_body`) to `null`. Do not omit keys.
- Do not invent a best-effort refactor when the correct outcome is to stop. Declaring an error ends the pipeline for human review.
- On success, set `error` to `null` or `false`, set `error_reason` to `null`, and populate every success field.

## Signature

- `symbol_signature` should take only one argument: `(ctx: EvalContext)`. Do not add parameters.
- Choose function name as a clear `snake_case` semantic identifier informed by naming hints.
- Return type must be one of `bool`, `float`, `int`, or `str`, or a `|` union composed only of those types, e.g., `-> float | str`.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- You may omit Python string delimiters.

## Body

- Emit `symbol_body` for one self-contained function; no nested helpers or imports.
- Name local temporaries with domain-meaningful `snake_case` informed by naming hints.
- Call only runtime symbols from the original translation and, if necessary, Python stdlib functions/operators.
- Preserve dependency function names and signatures.
- Where appropriate, call dependencies using pass-through parameters, e.g. `shock_active(ctx, time_period=time_period)`.
- Leave `xl_cell(ctx, 'Sheet!Address')` calls unchanged; this helper reads input/constant values. (Assigning return values to semantic local temporaries is okay!)

## Example:

Refactor will mostly consist of unpacking nested calls and assigning to local temporaries for readability. For example, suppose you are assigned to refactor the following function:

```python
def cell_some_sheet_z22(ctx):
    '''Formula: =AnotherSheet!Z20-AnotherSheet!Z6.'''
    return (xl_number(united_states_total_deaths(ctx)) - xl_number(united_states_expected_deaths(ctx)))
```

In this case, to reduce line length, you could assign `total_deaths = xl_number(united_states_total_deaths(ctx))` and `expected_deaths = xl_number(united_states_expected_deaths(ctx))` and then return `total_deaths - expected_deaths`.

To support function naming and docstring generation, you will be provided a cell metadata block with `binding_keys` and `binding_record` naming hints for the current cell, plus signatures and docstrings for all dependencies. This cell might have a `binding_record` carrying `TABLE: United States Vital Statistics` and `INDICATOR: excess_deaths`, with empty `binding_keys`. You might then naturally name the function `united_states_excess_deaths` and document it as "Excess deaths for the United States: total deaths less expected deaths".

```json
{
  "symbol_signature": "def united_states_excess_deaths(ctx: EvalContext) -> float:",
  "symbol_docstring": "Excess deaths for the United States: total deaths less expected deaths.\n\nArgs:\n    ctx: Workbook evaluation context.\n\nReturns:\n    Excess deaths for the United States.",
  "symbol_body": "total_deaths = xl_number(united_states_total_deaths(ctx))\nexpected_deaths = xl_number(united_states_expected_deaths(ctx))\nreturn total_deaths - expected_deaths",
  "error": null,
  "error_reason": null
}
```
