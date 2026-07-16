You will be provided a fingerprint summary of a cluster of Excel formula cells: one structural skeleton, reference relations for each ref slot (including counterpart dimensions sharing a concept), a complete member key space, and a single exemplar mechanical Python translation. Your task is to refactor the cluster into a single domain-aware parameterized Python function that selects operands by distinct binding dimension ids. The helper name is locked to `helper_name` from the cluster context (the binding `series_id`); do not invent a function name or emit a `def` line.

## Output format

Return only JSON matching the response schema:

```json
{
  "additionalProperties": false,
  "properties": {
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
    "symbol_docstring",
    "symbol_body",
    "error",
    "error_reason"
  ],
  "title": "ClusterRefactorLLMResponse",
  "type": "object"
}
```

Do not emit `parameters` or `member_keys`. The pipeline synthesizes both mechanically from `key_vocabulary` and the cluster's expected binding keys.

## Aborting

- If the cluster is unrefactorable or unrepresentable (for example contradictory membership), set `error` to `true` and provide a concise non-empty `error_reason`.
- When `error` is `true`, set every success field (`symbol_docstring`, `symbol_body`) to `null`. Do not omit keys.
- On success, set `error` to `null` or `false`, set `error_reason` to `null`, and populate every success field.
- Declaring an error stops the pipeline run for human review.

## Fingerprint context

- Each `## Fingerprint F…` block covers every member that shares one structural skeleton. There is no sampling: the key space lists all observed values.
- The backtick formula uses `ref_N[DIM,…]` placeholders. Reference relations describe how each `ref_N` slot's binding keys relate to the member's own keys, including counterpart dimensions (e.g. `COUNTERPART_REF_AREA = member.COUNTERPART_REF_AREA`).
- `reads:` lines say how to resolve the referenced cells (`xl_cell` address templates, semantic helpers, or in-cluster self-recurrence).
- The exemplar translation is one concrete `cell_*` body. Generalize from the relations + exemplar; do not assume other members are shown as source.

## Signature

- The pipeline synthesizes `def {helper_name}(ctx: EvalContext, …)` mechanically from the locked name and `key_vocabulary` — including counterpart dimension ids that share a concept with another parameter. Emit only docstring and body.
- Use `suggested_param_name` from `key_vocabulary` as each parameter's Python name; counterpart dimension ids yield distinct names (e.g. `ref_area` and `counterpart_ref_area`), so parameter names never collide.
- Series-constant binding keys (`scope: series`) are not parameters; bake them into the helper.
- The cluster has already been qualified by formula structure and binding-key shape at each reference position. Do not reinterpret its membership.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- Document each `snake_case` Python parameter in `Args` (not all-caps dimension id or concept).
- You may omit Python string delimiters.

## Body

- Emit `symbol_body` for one self-contained function; no nested helpers or imports.
- Parameterize the formula operand structure: use each dimension-id parameter to select the operand it governs (e.g. a lookup table from `ref_area` for one operand's row and from `counterpart_ref_area` for the other operand's row).
- Call only runtime symbols from the exemplar translation and, if necessary, Python stdlib functions/operators.
- Preserve dependency function names and signatures.
- Where appropriate, directly pass through parameters in function calls; e.g. `prior_period_total(ctx, reporting_period=reporting_period)`.
- Rename local temporaries to domain-meaningful `snake_case` informed by naming hints.
- Leave `xl_cell(ctx, 'Sheet!Address')` calls unchanged; this helper reads input/constant values. (Assigning the return value to a semantic local temporary is okay!)
- Prefer concise lookup tables over verbose `if`/`elif` ladders.
- Derive a reference value inside the helper when it follows mechanically from a member parameter (e.g. a fixed lag or period anchor switch). Never collapse two dimension ids onto one concept parameter.

## Example: bilateral flows with counterpart dimension ids

Suppose the dump shows fingerprint `=ref_0[REF_AREA,TIME_PERIOD]-ref_1[COUNTERPART_REF_AREA,TIME_PERIOD]` with identity relations on each role dim, locked `helper_name=bilateral_trade_balance`, and exemplar metadata carrying `REF_AREA`, `COUNTERPART_REF_AREA`, and `TIME_PERIOD`. Both operand rows are selected by their own dimension-id parameter.

```json
{
  "symbol_docstring": "Return exports minus imports for a reporter-counterpart area pair in a period.\n\nArgs:\n    ctx: Workbook evaluation context.\n    time_period: Period index (1 through 2).\n    ref_area: Reporting area code ('USA' or 'DEU').\n    counterpart_ref_area: Counterpart area code ('CHN' or 'FRA').\n\nReturns:\n    Exports of the reporting area minus imports from the counterpart area.",
  "symbol_body": "exports_row_by_area = {'USA': 4, 'DEU': 5}\nimports_row_by_counterpart = {'CHN': 6, 'FRA': 7}\ncolumn_by_period = {1: 'C', 2: 'D'}\ncolumn = column_by_period[time_period]\nexports_value = xl_number(xl_cell(ctx, f'Data!{column}{exports_row_by_area[ref_area]}'))\nimports_value = xl_number(xl_cell(ctx, f'Data!{column}{imports_row_by_counterpart[counterpart_ref_area]}'))\nreturn exports_value - imports_value",
  "error": null,
  "error_reason": null
}
```

## Naming conventions

To support docstring generation and parameterization, you will be provided a fingerprint summary (skeleton, reference relations, full key space, exemplar source), locked `helper_name`, `key_vocabulary`, exemplar `expected_keys` / `binding_keys` / `binding_record` naming hints, and dependency stubs. The function name, parameters, and per-member keys (including counterpart dimension ids) are filled mechanically; focus on the docstring and body.
