# Series bindings

Author `inputs.bindings.yaml`, `outputs.bindings.yaml`, and `internals.bindings.yaml` here before running the pipeline.

Use schema version `1.8.0` and the prompt in [templates/binding-authoring-prompt.txt](../templates/binding-authoring-prompt.txt).

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
