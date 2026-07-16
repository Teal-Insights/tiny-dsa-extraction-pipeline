You will be provided the context of a single-cell Excel formula and a mechanically assembled Python function body. The body is the verified mechanical translation of the cell with every `cell_*` dependency call rewired to its semantic helper: its structure, dependency calls, coercions, and laziness are final. Your task is only the semantic layer: write the docstring and rename the mechanical local temporaries to domain-meaningful names. The helper name is locked to `helper_name` from the cell context; the signature is synthesized mechanically as `def {helper_name}(ctx: EvalContext)`.

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
    "renames": {
      "anyOf": [
        {
          "items": {
            "additionalProperties": false,
            "properties": {
              "original": {"type": "string", "title": "Original"},
              "replacement": {"type": "string", "title": "Replacement"}
            },
            "required": ["original", "replacement"],
            "type": "object"
          },
          "type": "array"
        },
        {"type": "null"}
      ],
      "description": "Rename for every mechanical local in the draft body (all names starting with '_'). Null when error is true.",
      "title": "Renames"
    },
    "error": {
      "anyOf": [{"type": "boolean"}, {"type": "null"}],
      "description": "Set to true to abort this refactor and stop the pipeline when the draft cannot be meaningfully documented. Null or false on success.",
      "title": "Error"
    },
    "error_reason": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Human-readable explanation of why refactoring must abort. Non-empty when error is true; null otherwise.",
      "title": "Error Reason"
    }
  },
  "required": ["symbol_docstring", "renames", "error", "error_reason"],
  "title": "SingletonNamingLLMResponse",
  "type": "object"
}
```

Do not emit a function body, signature, or `def` line. The pipeline applies your renames mechanically to the assembled draft and rejects anything else.

## Aborting

- If the cell context is contradictory or the draft cannot be meaningfully documented, set `error` to `true` and provide a concise non-empty `error_reason`.
- When `error` is `true`, set every success field (`symbol_docstring`, `renames`) to `null`. Do not omit keys.
- On success, set `error` to `null` or `false`, set `error_reason` to `null`, and populate every success field.
- Declaring an error stops the pipeline run for human review.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- The helper takes only `ctx`; document it as the workbook evaluation context.
- Use the cell metadata (`binding_keys`, `binding_record`) and dependency docstrings to infer domain meaning.
- You may omit Python string delimiters.

## Renames

- Rename **every** local whose name starts with `_` (for example `_t1`). The listed renameable locals are the complete set; do not invent originals.
- Base names on what each local *holds*: the series ids in the dependency calls (`read_interest_baseline(...)` → `interest_rate_baseline`) and the cell metadata are your naming hints.
- Replacement names must be unique `snake_case` identifiers and must not start with `_`, collide with dependencies, runtime symbols, or Python builtins.
