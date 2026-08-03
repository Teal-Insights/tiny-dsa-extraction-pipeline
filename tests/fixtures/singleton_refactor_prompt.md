You will be provided a mechanical Python translation of a single-cell Excel formula. Your task is to refactor it as a domain-aware semantic function. The helper name is locked to `helper_name` from the cell context (the binding `series_id`); do not invent a function name or emit a `def` line.

## Output format

Return only JSON matching the response schema:

```json
{
  "additionalProperties": false,
  "properties": {
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
- When `error` is `true`, set every success field (`symbol_docstring`, `symbol_body`) to `null`. Do not omit keys.
- Do not invent a best-effort refactor when the correct outcome is to stop. Declaring an error ends the pipeline for human review.
- On success, set `error` to `null` or `false`, set `error_reason` to `null`, and populate every success field.

## Signature

- The pipeline synthesizes `def {helper_name}(ctx: EvalContext)` mechanically from the locked name. Emit only docstring and body.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- You may omit Python string delimiters.

## Body

- Emit `symbol_body` for one self-contained function; no nested helpers or imports.
- Mechanical codegen may already hoist nested `xl_*` calls into statement-level `_tN` temporaries. Rename every `_tN` (numbers may be large / non-local across the module) to domain-meaningful `snake_case` informed by naming hints. Do not leave opaque `_tN` names in the refactored body.
- Preserve evaluation order of those statement temps. Do not re-nest them into a single return expression.
- Preserve lazy / short-circuit shapes left nested by codegen (`IF` / `CHOOSE` / `IFERROR` thunks, `IS*` lambdas, DIV-guard lambdas). Do not eagerly evaluate them.
- Deduping repeated identical `_tN = xl_cell(ctx, 'Same!Addr')` loads into one semantic local is fine.
- Residual inline `xl_number(...)` / arithmetic on the return line is OK for readability.
- Call only runtime symbols from the original translation and, if necessary, Python stdlib functions/operators.
- Preserve dependency function names and signatures.
- Where appropriate, call dependencies using pass-through parameters, e.g. `shock_active(ctx, time_period=time_period)`.
- Leave `xl_cell(ctx, 'Sheet!Address')` calls unchanged; this helper reads input/constant values. (Assigning return values to semantic local temporaries is okay!)

## Example:

Mechanical source may already contain `_tN = …` statements. Your primary job is to rename those temps, preserve evaluation order, and clarify semantics — not to invent a different unpacking. For example, suppose you are assigned to refactor the following function with locked `helper_name=united_states_excess_deaths`:

```python
def cell_some_sheet_z22(ctx):
    """Formula: =AnotherSheet!Z20-AnotherSheet!Z6."""
    _t1 = united_states_total_deaths(ctx)
    _t2 = united_states_expected_deaths(ctx)
    return xl_number(_t1) - xl_number(_t2)
```

Rename `_t1` / `_t2` to domain-meaningful locals and keep the residual `xl_number(...)` coercions on the return line:

You will be provided a cell metadata block with `helper_name`, `binding_keys`, and `binding_record` naming hints for the current cell, plus signatures and docstrings for all dependencies. Document the locked helper using those hints (for example `TABLE: United States Vital Statistics` and `INDICATOR: excess_deaths`).

```json
{
  "symbol_docstring": "Excess deaths for the United States: total deaths less expected deaths.\n\nArgs:\n    ctx: Workbook evaluation context.\n\nReturns:\n    Excess deaths for the United States.",
  "symbol_body": "total_deaths = united_states_total_deaths(ctx)\nexpected_deaths = united_states_expected_deaths(ctx)\nreturn (xl_number(total_deaths) - xl_number(expected_deaths))",
  "error": null,
  "error_reason": null
}
```
