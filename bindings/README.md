# Series bindings

Author `inputs.bindings.yaml`, `outputs.bindings.yaml`, and `internals.bindings.yaml` here before running the pipeline.

Use schema version `1.10.0` and the prompt in [templates/binding-authoring-prompt.txt](../templates/binding-authoring-prompt.txt).

## Output compute helpers

When an internals helper covers a published output series' leaves, declare
`output.compute.helper` so generated `compute_*` calls the helper from record
dims instead of `xl_cell(address)` (excel-grapher schema 1.10.0):

```yaml
output:
  compute:
    name: compute_scenario_primary_expenditure_pct_gdp
    helper:
      name: scenario_primary_expenditure_pct_gdp_hot
      dims: [TIME_PERIOD]
```

`dims` defaults to the series `key` when omitted. Leaves without helper coverage
still use `xl_cell`. Declare helper blocks in the binding catalog for every
output series id that matches a same-named function in the generated
`internals.py`; `scripts/author_bindings.py` passes them through verbatim.

After authoring (or when export fails in codegen), run
`uv run python -m scripts.binding_resolution_audit`
([issue #101](https://github.com/Teal-Insights/extraction-pipeline-template/issues/101))
to catch bind-resolution errors that `validate_series_bindings` /
`derive_*_series` can miss — for example sparse year headers without
`fill: true`, or output series that resolve only a subset of their `data_range`.
Codegen requires resolution `ok=True`; validation alone does not. This is a
correctness audit of authored bindings; burndown
(`uv run python -m scripts.internal_binding_burndown`) is the coverage worklist for
cells that still lack a binding.

## Sparse labels, measure columns, and graph targets

Summary tables often place a year (or other group label) only on the first column
of a repeating measure triplet, with blank cells under the remaining measures:

```text
header row:  2050   (blank) (blank)  2075   (blank) (blank)  ...
measure row: Base   Alt     Gap      Base   Alt     Gap      ...
data:        ...    ...     ...      ...    ...     ...      ...
```

That layout trips three authoring mistakes that pass `validate_series_bindings`
but fail at output/input codegen:

1. **Sparse `column_header` / `row_label` cells need `fill: true`.**
   Without `fill`, blank label cells raise `bind_resolution_failed` for every
   column/row that has no source label, even when the contiguous `data_range`
   looked fine. Set `fill: true` when labels appear only on the first
   column/row of a group and should propagate across the blank span.

2. **Do not bind one contiguous rectangle across mixed measures** when the
   public series key is only something like `(SCENARIO, TIME_PERIOD)`.
   Columns from different measures then collide on the same key, or only a
   subset of columns resolve. Prefer either:
   - **one shard per measure column** (or per milestone column), sharing the
     same `output.compute.name` / `input.setter.name` when export should merge
     shards into one public function; or
   - a **richer key** that includes the measure dimension (common for
     internals that triangulate the whole triplet table).

3. **`workbook_config.TARGETS` (and overlapping internal ranges) must cover
   every bound data cell.** Sharding a “gap-only” column that sits past the
   previous target end requires widening the target (and any internal
   `data_range` that must stay on-graph for those cells). Bound cells outside
   the extracted graph never resolve cleanly.

Pedagogical catalog fragment (not used by the synthetic smoke workbook):
[templates/binding-pattern-measure-shards.example.yaml](../templates/binding-pattern-measure-shards.example.yaml).

## Dimension `id` vs `concept`

| Field | Role |
|---|---|
| `concept` | SDMX-style meaning category (e.g. `TIME_PERIOD`) |
| `id` | Dimension identity used in records, cell keys, and refactor parameters |

Give every dimension an explicit `id`. When `id` is omitted, the effective id falls back to `concept`. If two dimensions share a concept, they must have distinct ids:

```yaml
dimensions:
  - id: PROJECTION_PERIOD
    concept: TIME_PERIOD
    role: key
    scope: cell
    bind:
      kind: column_header
      header_row: 5
      read: int
  - id: REFERENCE_PERIOD
    concept: TIME_PERIOD
    role: key
    scope: cell
    bind:
      kind: value_map
      values:
        0: C:G
      read: int
key: [PROJECTION_PERIOD, REFERENCE_PERIOD]
```

Effective ids drive parameter names (`projection_period`, `reference_period`). Concepts remain semantic metadata for documentation and concept-scheme dtype inheritance.

## Cluster refactor contracts

The LLM cluster-refactor step selects one of two contracts from each cluster's shape (`src/refactor_contracts.py`):

| Contract | Applies when | Prompt fixture |
|---|---|---|
| A — member sweep | Every formula operand is derivable from the member cells' own sweep keys, including constant lags/offsets like `t - 1` (the default; always the case under `variation_mode: dominant_key_only`) | `tests/fixtures/cluster_refactor_prompt.md` |
| B — dimension aware | Some formula operands instead route through counterpart dimension ids that share a concept with a member key (e.g. `REF_AREA` + `COUNTERPART_REF_AREA`) | `tests/fixtures/cluster_refactor_prompt_dimension_aware.md` |

Selection checks each operand reference position: its binding key value must equal a member-cell key (plus one shared constant offset for numeric keys) for some member dimension id sharing the concept. Positions derivable from the member's own key stay on Contract A; positions needing a counterpart dimension id select Contract B.

Under Contract A, helper parameters are exactly the varying member sweep keys; derivable lags stay in the helper body, and validation rejects invented counterpart parameters. Under Contract B, parameters and `member_keys` are keyed by effective dimension id, so two parameters may share one concept; validation rejects responses that collapse distinct dimension ids onto a single concept parameter.

When any operand position cannot be routed this way — for example three independently varying `REF_AREA` operand roles with only two declared dimension ids — the cluster is skipped as `operand_level_variation_unsupported`. To make such a cluster refactorable, declare one counterpart dimension (distinct `id`, shared `concept`) per independently varying operand role on the internal series that binds the member cells. `variation_mode` only controls whether such clusters are formed at all: `dominant_key_only` splits them away during clustering, while `independent` keeps them together for Contract B.
