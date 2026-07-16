You will be provided a fingerprint summary of a cluster of Excel formula cells and a mechanically synthesized Python function body. The body has already been verified correct against every member of the cluster: its parameterization, lookup tables, dependency arguments, and branch routing are final. Your task is only the semantic layer: write the docstring and rename the mechanical local temporaries to domain-meaningful names. The helper name is locked to `helper_name` from the cluster context; the signature and body structure are synthesized mechanically.

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
      "description": "Rename for every mechanical local in the draft body (all names starting with '_'); lookup-table renames are optional. Null when error is true.",
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
  "title": "ClusterNamingLLMResponse",
  "type": "object"
}
```

Do not emit a function body, signature, parameters, or member keys. The pipeline applies your renames mechanically to the verified draft and rejects anything else.

## Aborting

- If the cluster context is contradictory or the draft cannot be meaningfully documented, set `error` to `true` and provide a concise non-empty `error_reason`.
- When `error` is `true`, set every success field (`symbol_docstring`, `renames`) to `null`. Do not omit keys.
- On success, set `error` to `null` or `false`, set `error_reason` to `null`, and populate every success field.
- Declaring an error stops the pipeline run for human review.

## Docstring

- `symbol_docstring` must include a Google-style docstring with a semantic description and `Args` and `Returns` sections.
- Document each `snake_case` Python parameter in `Args` (not all-caps dimension id or concept), including its observed value range from the member key space.
- Use the fingerprint summary, member metadata (`binding_keys`, `binding_record`), and dependency docstrings to infer domain meaning.
- You may omit Python string delimiters.

## Renames

- Rename **every** local whose name starts with `_` (for example `_t1`, `_f2_t3`) to a domain-meaningful `snake_case` name. The listed renameable locals are the complete set; do not invent originals.
- Locals prefixed `_f1_`, `_f2_`, … belong to different fingerprint branches of the same helper (for example a first-period anchor branch versus the recurrence branch). Give each branch's locals distinct names; a short branch hint prefix (such as `initial_`) is welcome when the same quantity appears in both branches.
- Base names on what each local *holds*: the series ids in the dependency calls (`read_interest_baseline(...)` → `interest_rate_baseline`) and the member metadata are your naming hints.
- Mechanical lookup-table names (for example `column_by_time_period`) may be renamed when a clearer domain name exists; otherwise omit them from `renames`.
- Replacement names must be unique `snake_case` identifiers and must not start with `_`, collide with the helper's parameters, dependencies, runtime symbols, or Python builtins.
