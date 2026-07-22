# Technical Standard

---

## Good Extraction

This standard defines when an Excel-to-Python extraction is *good enough to ship*. It covers configure → extract → export → refactor.

---

### Summary

A good extraction produces a distributable Python library whose **public API is semantic and economist-facing**, whose **dependency graph is complete and constraint-resolved**, and whose **generated code is mechanically traceable** back to workbook cells. Internals must be **organized into functions that map to macrofinance computations**, not individual Excel workbook cells. The library must be **runnable without Excel** but must produce the **same outputs as the original workbook for the same inputs**. The library must be documented with **detailed docstrings, a polished website, and a GitHub README**.

---

### Stage gates

Each gate has a default owner role. Adapt names to your team; the responsibilities stay the same.

| Role | Signs off on |
|---|---|
| **Config author** | Ingest, configure, extract, document, and refactor stages — targets, bindings, constraints, and committed parity evidence |
| **Graph reviewer** | Extract completeness — manual graph review and optional LLM dependency audits |
| **Parity owner** | Export and test — full pipeline run and differential parity (including Windows Excel when available) |

#### 1. Configure

| Criterion | Pass condition |
|---|---|
| **Targets declared** | Every published output is a named target (range name or sheet-qualified address) driving target-driven graph extraction. |
| **Series bindings authored** | `bindings/inputs.bindings.yaml` and `bindings/outputs.bindings.yaml` exist, use `schema_version: 1.10.0`, and declare one logical scalar/series/table per public I/O function. Every dimension should have an explicit `id`; record/key fields and refactor parameters use the effective dimension id, with concept as semantic metadata and unambiguous fallback. |
| **Bindings validated against graph** | `validate_series_bindings(...)` reports `ok`; input bindings overlap graph leaves, output bindings overlap target nodes. |
| **Dynamic refs resolved** | All `OFFSET` / `INDEX` / `MATCH` / `CHOOSE` dependencies are resolved via `DynamicRefConfig.from_constraints(...)` without `DynamicRefError`. |
| **Every mutable leaf is bound** | Each leaf classified as `input` appears in `inputs.bindings.yaml`; unbound mutable leaves fail the pipeline. |
| **Constants distinguished from inputs** | Single-value `Literal[...]` constraints mark lookup/structural data; range constraints mark user-editable inputs. |
| **Constraints cover all leaves** | Every graph leaf has a typed constraint (`Literal`, `Between`, `RealBetween`, etc.) for codegen, testing, and documentation. |
| **Public input labels resolved** | Every scenario value written to an enum public input cell matches a workbook reference label exactly. A configure test fails if any scenario input cannot be resolved or if `CONSTRAINTS` literals diverge from reference cells. |

**Address keys:** `excel-grapher` stores sheet-qualified addresses in canonical form (e.g. `'Discrete Risks'!H2`). Human-authored `CONSTRAINTS` keys and graph `leaf_keys()` may differ in quoting but normalize to the same form via `normalize_cell_type_env_key()` (constraint matching) and `normalize_key()` (graph lookup). Harnesses and audits must normalize before comparing config addresses to graph keys. See [issue #51](https://github.com/Teal-Insights/extraction-pipeline-template/issues/51).

**Workbook-exact label resolution:** Scenario matrices and exported APIs naturally use clean logical values (`"High"`, `"Real interest rate"`). Many workbooks branch with **exact string equality** on reference label cells (e.g. `IF($B$2=$B$9, …)`). If differential/configure harnesses write logical strings to public input cells instead of the workbook's exact label literals, formulas can silently fall through to `""` and produce `#VALUE!` on later projection years — while parity still passes when both oracles error the same way. This is distinct from address-key normalization ([#51](https://github.com/Teal-Insights/extraction-pipeline-template/issues/51)).

Treat **workbook reference label cells** as the source of truth for values written to public input cells. Logical scenario ids and API parameter names may stay human-readable; **Excel-facing writes must match reference literals character-for-character** (including trailing spaces and suffixes like `(a)`).

Configure checklist (workbook-neutral):

1. **Discover reference labels** — Identify cells the workbook uses in IF/MATCH/CHOOSE guards for each public enum input. Sheet and range names are project-specific.
2. **Audit exact literals** — Record values with `repr()`; do not assume trimmed or canonical spellings.
3. **Align `CONSTRAINTS`** — `Literal[...]` on each public input must use those exact strings, not cleaned-up scenario names.
4. **Resolve at the harness boundary** — Scenario definitions keep logical values; a small resolver maps logical → workbook-exact immediately before golden/graph `set_inputs`. Compare with `.strip()` (and prefix rules for suffix variants like `"Real interest rate"` → `"Real interest rate (a)"`); **write the raw workbook string**.
5. **Normalize only internally** — Block lookups, dict keys, and test ids may strip; public input writes may not until the source workbook is corrected.
6. **Configure test** — Assert every scenario value for each enum public input resolves to a loaded workbook label, and that every `CONSTRAINTS` literal appears among reference labels. See [`tests/differential/workbook_labels.py`](tests/differential/workbook_labels.py).

`tests/test_workbook_constraints.py` enforces the configure-stage binding and constraint invariants above against the synthetic workbook fixture in CI. When `REFERENCE_LABEL_CELLS` is populated in `workbook_config.py`, `tests/test_workbook_labels.py` enforces label resolution.

#### 2. Extract

| Criterion | Pass condition |
|---|---|
| **Graph builds cleanly** | `create_dependency_graph(..., load_values=True, dynamic_refs=..., capture_dependency_provenance=True)` succeeds. |
| **Graph is inspectable** | DAG from outputs to inputs; manual review confirms expected sheets, no spurious nodes, no missing shock/engine paths. |
| **Provenance captured** | `capture_dependency_provenance=True` so later compression/refactor projections are safe and auditable. |
| **Series derive cleanly** | `derive_input_series` / `derive_output_series` resolve every binding to concrete cell addresses. |
| **Dependency chains pass AI-powered spot-checking** | Optional: declare `GRAPH_AUDIT_CASES` in `workbook_config.py` (loaded via `PipelineConfig.graph_audit_cases`), set `LLM_GRAPH_AUDIT_MODEL` (defaults to `gpt-5.5`; name prefix selects OpenAI, Z.AI, or DeepSeek), and run `pytest tests/test_extraction_graph_accuracy.py --run-skipped` with the matching provider API key. The synthetic smoke-test audit runs without copying cases into `workbook_config.py`. Per-parent audits spot-check direct dependency sets; they do not exhaust every conditional path. `LLM_GRAPH_AUDIT_CASES` optionally caps how many declared cases are selected per run. Audits skip the LLM and return `inconclusive` when dependency evidence is truncated; returned addresses are normalized and validated (`spurious_dependencies` must be direct graph children; unknown addresses are flagged separately). Only `verdict: "correct"` counts as a pass. |

#### 3. Export

| Criterion | Pass condition |
|---|---|
| **Leaf classification attached** | Before codegen, every graph leaf is classified `input` or `constant` and attached to the graph. |
| **Records-shaped public API** | Codegen emits `make_context()`, `set_*` input setters, and `compute_*` output functions from series bindings—not raw cell writers. |
| **Inputs validated at runtime** | Setters validate record shape and key matching; domain/units prose belongs in docstrings, not implied runtime validation beyond what codegen emits. |
| **Domain-language identifiers** | Public functions **and** internal functions use macrofinance vocabulary (`growth_baseline`, `output_delta`), not workbook coordinates (`U24`, `OFFSET_RANGE_3`). |
| **Concise/readable code** | Internal formula cell groups are collapsed to functions, rewritten with macrofinance semantics, parameterized by binding dimension ids (with concept as semantic metadata), and reused to reduce code duplication. |
| **Pandas/Polars compatible** | Public functions can accept (and ideally return) pandas or polars `DataFrame`s as inputs as well as scalars, sequences, and `Records` lists. |
| **Docstrings on public API** | Every `set_*` and `compute_*` has a docstring: deterministic fields from the binding contract, LLM-authored prose from a registered docstring callback grounded in the human guide. |
| **Distributable package** | Export writes `dist/<package>/` with `api.py`, runtime modules, `pyproject.toml`, and README; package imports without the extraction repo on `PYTHONPATH`. |
| **Validation bundle shipped** | Differential harness, workbook fixture, and reference parity reports (with 100% passing scores) are exported under `dist/tests/`. |
| **Documentation website published** | `dist/website/` contains a polished website with detailed macrofinance explanations and usage instructions and examples. |

---

### Acceptance bar

An extraction meets this standard when:

1. All stage-gate checks above pass programmatically or by documented human review where judgment is required (graph completeness).
2. The exported library runs a representative scenario end-to-end using only the semantic API.
3. A macrofinance domain expert can configure inputs and read outputs without opening the workbook.
4. The extraction repo documents *decisions* (targets, binding choices, constraint domains, constant vs input classification) clearly enough that an agent or teammate can replicate the process on a new workbook.

Golden-master parity (100% pass rate, precision policy, first-divergence reporting) is required for release.

---

### Known gaps/footguns

- **Binding authoring needs a scaling strategy:** Larger workbooks need a structured discovery workflow (logical tables → series catalog → graph cross-check); the prompt pattern in the pipeline doc is the reference.
- **User override of formula cells is not currently allowed**: Currently we're enforcing that all input cells must be leaf nodes. However, there's at least one user-editable cell in the LIC DSF that is not a leaf node, so we will need to relax this constraint for the LIC DSF extraction.
- **Synchronous LLM API calls slow down the pipeline**: Currently we're calling LLMs synchronously at each stage of the pipeline. For large workbooks, we will need to parallelize LLM calls to speed up the pipeline. (In some cases, sequencing is important, so we'll have to do this intelligently.)
- **LLM-authored configs and docstrings are not currently validated**: We may want to run some evals over the AI-generated series bindings and docstrings to make sure this is really the API shape we want.
- **Context-passing is a bit unergonomic**: We're currently requiring the user to pass the context object to every function. This sits uncomfortably between functional and object-oriented programming paradigms, so we should commit to one or the other (e.g., attach public functions to the context object as methods).
- **Error handling is insufficiently Pythonic**: Our Python runtime replicates Excel error-handling semantics. In Excel, errors in "internals" are made visible via error codes like `#N/A` and `#VALUE!` appearing in user-visible cells. In Python, internals are hidden from the user, so we should raise Python exceptions instead.
- **Excel runtime still uses ugly helpers for simple mathematical operations**: Where possible, we should use Python's built-in mathematical operators. This should be doable for adding, subtracting, and multiplying, but may not be possible for division (because Excel division coerces datatypes differently). (Perhaps we could implement division by wrapping operands in coercion functions like `float` or `int`.)
- **Public API takes pandas/polars inputs but does not return pandas/polars outputs**: We should provide a way to return outputs as pandas/polars DataFrames if that's what the user specifies.
- **Dynamic ref resolution is not fully implemented for hard cases yet:** We don't yet fully support nested dynamic refs in `excel-grapher`, and constraint resolution can take a long time for wide domains due to combinatorial blowup.
- **Similarity-aware graph packing should be explored as a better compression strategy:** Export uses `OptimalCompression` as the compression strategy; this seemed to work well on Tiny DSA, but similarity-aware compression might be better for larger workbooks (to maximize deduplication potential).

---

### Checklist (copy for new workbook)

Ordered to match the onboarding checklist in [README.md](README.md#clone-and-configure-onboarding-checklist):

[ ] Ingest: workbook and guide populated; stale bindings, dist/, and .cache/ cleared
[ ] Audit: pre-extraction workbook audit reviewed; blocking automation resolved
[ ] Configure: outputs declared as extraction targets
[ ] Configure: bindings/inputs.bindings.yaml + outputs.bindings.yaml validated
[ ] Configure: dynamic-ref constraint candidates constrained
[ ] Configure: all leaves classified; mutable leaves bound
[ ] Configure: public enum input labels resolved to workbook reference literals
[ ] Extract: graph extracts with provenance (--extract-graph)
[ ] Review graph: manual completeness review done; optional LLM dependency audit passed
[ ] Verify graph: scenario matrix defined in tests/differential/; graph-oracle parity passes (uv run python -m tests.differential.differential_test_graph)
[ ] Export: dist package builds; semantic API scenario runs
[ ] Export: validation bundle exported; exported-library differential parity passes
[ ] Document / refactor: public API uses domain language; docstrings present
[ ] Document / refactor: internals refactored; parity re-confirmed

Generated graph artifacts under `artifacts/dependency-graph/` are gitignored; workbook audit reports may be committed optionally. See [artifacts/README.md](artifacts/README.md).

---

## Golden-Master Validation

---

### 0. What this standard governs

Golden-master (a.k.a. differential) validation feeds **identical inputs** to two
oracles and asserts they produce the **same outputs**. When they disagree, the trusted
oracle wins and the disagreement localizes a bug in the system under test. (McKeeman,
*Differential Testing for Software*, 1998.)

| Role | Definition for this standard |
|---|---|
| **Golden Master** (reference oracle) | The trusted Excel workbook, driven live through Microsoft Excel via `xlwings` (COM automation). Source of truth. |
| **System Under Test (SUT)** | The generated Python artifact. Two layers may be validated: the *extracted computation* (the in-memory dependency graph, evaluated directly) and the *shipped package* (the standalone library exercised only through its public API). Each answers a different question — "is the extraction faithful?" vs. "is the artifact we hand users faithful?" |
| **Parity report** | The two-file (CSV + TXT) record of every cell comparison, defined in §2. |
| **Acceptance bar** | 100% of comparisons pass, at the precision policy of §1. Any single mismatch = FAIL. |

A conforming validation for a new tool MUST satisfy all of §1–§5. §6–§7 are the
per-tool application notes for Q-CRAFT v2 and DDT. §8 records the failure modes a green
checkmark does **not** cover, so they are designed in from the start rather than
discovered later.

---

### 1. Precision policy (the comparison contract)

This is the heart of the standard: the exact, ordered rule by which one output cell's
golden value and SUT value are judged equal.

#### 1.1 Absolute tolerance: `atol = 1e-6`

The numeric acceptance criterion is **absolute** difference within `1e-6`:

```
passed  ⇔  abs(golden - sut) <= 1e-6
```

It MUST be defined once as a single named constant and threaded through the run as
configuration — never inlined at a comparison site, so a tool's tolerance is auditable
in one place.

**Rationale.** Spreadsheets typically report values to ~6 decimals, and Excel and Python
legitimately differ in the last few bits from floating-point rounding, not from a real
defect. `1e-6` absorbs that rounding without masking a genuine divergence. In practice
the observed absolute difference on a faithful extraction is `0.0` (bit-exact) across the
passing domain — the tolerance is headroom, not a crutch.

**A relative difference is recorded but is NOT part of the pass/fail decision.** It is
computed (`abs_diff / abs(golden)`, or infinite when the golden value is zero) and written
to the report for triage only. The accept/reject gate is absolute tolerance alone. A new
tool MUST NOT switch the gate to relative tolerance without an explicit, documented
reason, because doing so changes what "parity" means and breaks comparability of reports
across tools.

#### 1.2 The ordered comparison ladder

Comparison is a **decision ladder**, evaluated top to bottom; the first matching rule
decides. Order matters — the `None` and non-finite checks must precede the numeric
tolerance check so a blank, an error, or a `NaN` never reaches the arithmetic.

| # | Condition | Verdict | Diffs recorded |
|---|---|---|---|
| 1 | both blank (`None`) | **pass** | `0.0` / `0.0` |
| 2 | exactly one side blank | **fail** | none |
| 3 | value not numeric-coercible (e.g. a spreadsheet error) | pass **iff** the two sides are equal by typed identity | none |
| 4 | numeric but non-finite (`NaN`, `±inf`) | pass **iff** exactly equal, **or** both `NaN` | none |
| 5 | finite numbers | pass **iff** `abs(golden - sut) <= atol` | computed |

Rules 1–2 are the **blank rules** (§1.4). Rule 3 is **error-class equality** (§1.3).
Rule 4 is the **`NaN` rule** (§1.4). Rule 5 is the **tolerance rule** (§1.1).

#### 1.3 Error-class equality

Spreadsheet error cells (`#DIV/0!`, `#VALUE!`, `#N/A`, …) are **first-class comparison
values**, not noise. Two cells that are both errors match **iff they are the same error
class**; a `#DIV/0!` on one side and a `#N/A` on the other is a **failure**, not a pass.

**Standard requirement.** The equality used for errors MUST be a *typed, class-level*
equality — the two sides agree on *which* error and are both recognized as errors. Do
**not** rely on incidental string equality between the spreadsheet driver's error text
and a language-level sentinel. Normalize the driver's raw error representation to a typed
error value first, then compare types. (A prior review found this working only by
accident on the pilot tool, where the error sentinel happened to be a string subclass
whose members spelled the Excel errors exactly. A new tool should make the error
comparison explicit from day one and, per §5, actually exercise it.)

**Matched errors fail the run unless declared.** Two cells that agree on the same error
class pass the *comparison*, but that comparison exercised nothing — both oracles
errored identically, so it contributes no evidence of computational parity. (Dropdown
label mismatches, e.g. logical `"High"` vs workbook `"High "`, are a common cause of
silent matched errors on long-horizon outputs only.) A matched error on a scenario that
does not declare `expects_error_values=True` MUST fail the *run* (exit code `1`) and be
listed in the report. Harnesses MAY offer a triage escape hatch
(`--allow-matched-errors`) that downgrades these to warnings; it MUST NOT be the
default, and CI MUST NOT pass it.

#### 1.4 Blank and `NaN` rules

- **Blank (`None`) rules:** both blank → pass; one blank against a value → fail. A
  blank-vs-number mismatch is a real divergence (a cell the SUT failed to populate, or
  populated when the golden master left it empty) and MUST fail, never be silently
  tolerated.
- **`NaN` / non-finite rules:** `NaN` compares false against everything, so it cannot be
  judged by tolerance and MUST be handled before the arithmetic. Two `NaN`s are treated as
  **equal**; `±inf` matches only its exact counterpart.

#### 1.5 Crash containment

A per-scenario exception MUST NOT abort the sweep. It is recorded as one **failing**
comparison per output cell, with both oracle values replaced by the exception's
class + message and the diff fields empty. The report thus captures a crash as a
first-class failure rather than losing the whole run.

---

### 2. Parity-report convention

Every conforming run writes **two files** to a report directory: a machine-readable CSV
(one row per cell comparison) and a human-readable TXT summary. Both are required; they
serve different readers (diffing/pivoting vs. eyeballing).

#### 2.1 CSV — one row per (scenario, cell)

Recommended schema:

```
scenario_id, cell_address, cell_label, golden_value, sut_value, abs_diff, rel_diff, passed
```

**Standard requirements for the CSV:** exactly one row per cell comparison; a stable
scenario/point identifier; the cell address **and** a human-readable label; both oracle
values; `abs_diff` and `rel_diff`; and a boolean pass column. Single-axis scenario
identifiers MUST be constructed so a per-axis pass-rate is recoverable by grouping the CSV
(e.g. name them `single_axis:<axis>=<value>`).

#### 2.2 TXT — the summary a human reads first

Required content, in order:

1. **Header:** title, generation timestamp (UTC, ISO-8601, seconds precision), workbook
   identity, SUT identity, and the tolerance (`atol = 1e-06`).
2. **Aggregate counts:** total comparisons, passed, failed, pass rate `%`.
3. **The acceptance bar, printed literally:** `Acceptance bar: 100.00%`.
4. **Result:** `PASS` or `FAIL`.
5. **First divergence** (§3), when any failure exists.
6. **The full list of failing comparisons.**

The TXT SHOULD also carry **per-axis** and **per-point** pass-rate tables so "which axis
broke" is readable at a glance, and — where the SUT is the extracted graph — an
**absent-inputs** section listing any input cell the sweep tried to set but the SUT does
not expose (itself a differential signal about the extraction).

#### 2.3 The 100% pass-rate acceptance bar

The bar is **100%**: any single mismatch fails the run. This MUST be asserted in three
places, and all three MUST agree:

- printed in the TXT (`Acceptance bar: 100.00%`);
- the process **exit code** — `0` iff every comparison passes and no undeclared matched
  errors remain (§1.3), `1` if any fail, `2` if a prerequisite is missing (no workbook,
  no Excel, package not generated);
- CI (`.github/workflows/test.yml` on pull requests) and reviewers treat any non-`PASS` report as a blocking failure.

#### 2.4 Reports are versioned artifacts, not scratch output

A parity report is only meaningful for the exact workbook, extraction configuration, and
sweep that produced it. When any of those change, the affected sweep MUST be re-run and
the refreshed report committed alongside the tool. A stale report silently describes a
model that no longer exists. Ship a reference report with the tool and treat a diff
against it as a regression signal.

---

### 3. First-divergence reporting convention

When a run fails, the TXT names the **single most actionable starting point**: the
*first* failing comparison in deterministic scan order (scenario order × cell-label
order).

The block MUST name the **first axis-point/scenario and the exact cell that diverged**,
with enough to reproduce and localize:

```
First divergence:
  scenario:  <scenario_id>
  cell:      <cell_address>  (<cell_label>)
  golden:    <golden_value>
  sut:       <sut_value>
  abs_diff:  <abs_diff>
  rel_diff:  <rel_diff>
```

**Why first, and why deterministic.** Because the scenario sweep and the cell-label order
are fixed and reproducible, "first divergence" is stable across runs on the same inputs —
the same bug surfaces at the same coordinate every time, so an engineer jumps straight to
one scenario + one cell instead of triaging thousands of rows. A conforming tool provides
at least this deterministic first-divergence pointer, and MAY additionally list all
failures ranked by descending absolute difference to surface the largest divergence.

---

### 4. Structural pre-flight checks (fail fast, before any scenario)

These run before the sweep and separate distinct failure causes, so a green run means
what it says. All three are required.

1. **Path / package verification.** The workbook and the generated package must exist;
   otherwise raise immediately with a regeneration hint. No sweep runs against missing
   inputs.
2. **Staleness check.** If the workbook is newer than the SUT's embedded snapshot of the
   model's constants, flag it: a later diff could be a staleness artifact rather than a
   codegen bug. This SHOULD be escalated to a content-hash comparison that **hard-fails**
   on mismatch — a warn-only gate can let a real drift through unnoticed.
3. **Binding-cell verification.** Assert that the SUT's input setters and output readers
   target exactly the cell addresses the sweep declares, before comparing any values.
   This **separates binding-correctness from calculation-correctness**: without it, a
   wrong-cell binding bug can be masked when the wrong cell's default happens to equal the
   test value. A conforming tool MUST verify its input/output cell bindings before
   trusting a passing calculation.

---

### 5. Coverage design (what the sweep must exercise)

The comparison contract is only as strong as the input domain it is quantified over. The
standard sweep is built from four scenario groups:

- **Canonical scenarios** — the tool's headline configurations (e.g. baseline + each
  named shock across each modeled entity). Anchors the sweep to real, meaningful cases.
- **Single-axis isolation** — perturb exactly one parameter around canonical, so a break
  shows up cleanly in the per-axis pass-rate table.
- **Full categorical factorial** — the Cartesian product of the categorical axes, to
  catch 2- and 3-way interactions single-axis sweeps cannot reach.
- **Boundary coverage** — the endpoints of any recursion/time dimension (the first and
  last periods, not just an interior one), where off-by-one and boundary bugs live.

**Mandatory addition for new tools — the error and boundary domain.** The single most
important lesson carried forward is that a happy-path-only numeric sweep is the *weakest*
differential test: divergences cluster at the edges, and the edges are exactly what a
well-formed-inputs sweep excludes. A conforming sweep for Q-CRAFT v2 / DDT MUST include an
**error/boundary scenario group** that drives every reachable error-return path and domain
edge — e.g. out-of-range dispatch indices, unknown lookup keys (`#N/A`),
denominator-zeroing inputs (`#DIV/0!`), never-firing / always-firing triggers, and
non-integer inputs that hit truncation. This is what makes error-class equality (§1.3)
actually *exercised* rather than merely defined.

---

### 6. Applying the standard to Q-CRAFT v2

Q-CRAFT (Quantitative Climate Risk Assessment Fiscal Tool; Batini et al., 2024) is a
larger IMF FAD workbook with 100+ country datasets and richer scenario logic than the
pilot. The standard scales without change to its **contract**; only the **tool-specific
map** changes.

**Held constant (do not re-derive per tool):**

- The precision policy of §1 verbatim — same `atol = 1e-6`, same comparison ladder, same
  error-class / blank / `NaN` rules. Keeping this identical is what makes a Q-CRAFT parity
  report comparable to any other.
- The two-file parity-report convention (§2), the 100% bar and exit codes (§2.3), the
  first-divergence block (§3), and the three pre-flight checks (§4).

**Re-specified per tool (the porting work):**

- **Named-range / binding map** — Q-CRAFT's input and output cells and their typed input
  shape. Verify them via the binding-cell check (§4.3) before trusting results.
- **Scenario axes** — Q-CRAFT's categorical axes (country, climate-shock type/severity,
  horizon) and continuous baselines, plus the mandatory error/boundary group (§5). The
  country-lookup pattern the pilot rehearses is the same mechanism Q-CRAFT uses at scale,
  so unknown-country `#N/A` coverage ports directly.
- **Output cell set** — the indicator/threshold and ratio-path cells Q-CRAFT reports.

Because Q-CRAFT loads 100+ country datasets, expect the sweep's scenario count to grow by
an order of magnitude; the report convention (per-axis tables, first-divergence, CSV
grouping) is designed to stay readable at that size and needs no structural change.

### 7. Applying the standard to DDT

DDT (the second target tool named in TEA-643) inherits the identical contract. The same
partition applies: **hold constant** the precision policy, report convention, acceptance
bar, first-divergence rule, and pre-flight checks; **re-specify** only the binding map,
the scenario axes (including the error/boundary group), and the output cell set for DDT's
workbook. If DDT's output includes categorical/classification cells (not just numerics),
those are handled by ladder rule 3 (non-coercible → typed equality) — the standard already
covers non-numeric outputs, so no new comparison rule is needed; only DDT's set of valid
categorical values must be enumerated in its scenario design.

**Conformance test for either tool:** a reviewer can confirm DDT/Q-CRAFT validation is "to
standard" by checking that (a) `atol` is `1e-6` and centralized, (b) the comparison ladder
matches §1.2 in order, (c) both report files exist with the §2 columns/sections, (d) the
TXT prints a 100% acceptance bar and the process exit code honors it, (e) a forced failure
produces a well-formed first-divergence block, (f) all three pre-flight checks run, and
(g) the sweep includes an error/boundary group.

---

### 8. What a green "100% PASS" does NOT prove

Documented so the next tool designs these in rather than discovering them later:

- **Untested input domain.** Parity is only quantified over the scenarios in the sweep. A
  100% bar on happy-path inputs proves "matches Excel on nice inputs," not "matches Excel."
  §5's error/boundary group is the mitigation and is mandatory for new tools.
- **Incidental error equality.** If error comparison rides on string identity rather than
  typed equality (§1.3), it is unverified until an error path actually runs. Make it
  explicit and exercise it.
- **Warn-only staleness.** A staleness *warning* (§4.2) can be ignored; a real
  workbook/snapshot drift can then be absorbed silently. Escalate to a content hash + hard
  fail.
- **Environment drift.** The reference report's Python/Excel environment should be recorded
  and, ideally, reproduced; parity can be environment-sensitive at the last bits.

---

### Conformance checklist — is a harness "to standard"?

A reviewer confirms a differential harness for Q-CRAFT v2, DDT, or any future Excel→Python
target conforms to this standard by verifying every item below in one pass. Each item is a
property to *check on a finished harness*, not a build step; the `(§x)` back-reference names
the section it enforces. Any unchecked box = not conformant.

#### Precision contract (§1)

- [ ] `atol` is `1e-6`, defined **once** as a named constant and threaded as configuration —
  not inlined at any comparison site (§1.1).
- [ ] The pass/fail gate is **absolute** tolerance alone; `rel_diff` is recorded for triage but
  does not decide pass/fail (§1.1).
- [ ] The comparison ladder matches §1.2 **in order** — blank → error-class → NaN/non-finite →
  tolerance, first-match-wins, with `None`/non-finite checked before the arithmetic (§1.2).
- [ ] Errors compare by **typed class** (`#DIV/0!` ≠ `#N/A`), via normalized typed error values
  rather than incidental string identity (§1.3).
- [ ] A matched error on a scenario without `expects_error_values=True` fails the run and is
  listed in the report; any `--allow-matched-errors` escape hatch is opt-in and absent from
  CI (§1.3).
- [ ] Blank rules hold: both blank → pass; exactly one side blank → fail (§1.4).
- [ ] NaN/non-finite are handled before the arithmetic: two `NaN` → equal, `±inf` matches only
  its exact counterpart (§1.4).
- [ ] A per-scenario exception is contained as a **failing** comparison (not an aborted sweep),
  with oracle values replaced by the exception class+message (§1.5).

#### Report & result (§2–§3)

- [ ] **Both** report files exist: a CSV with the §2.1 columns (one row per (scenario, cell),
  groupable single-axis ids) and a TXT with the §2.2 sections in order (§2.1–§2.2).
- [ ] The TXT prints `Acceptance bar: 100.00%`, the process **exit code** honors the bar
  (`0` pass / `1` fail / `2` prerequisite missing), and CI blocks on any non-`PASS` (§2.3).
- [ ] A forced failure produces a **well-formed, deterministic first-divergence block** — first
  failure in scenario × cell-label order, with reproduce/localize fields (§3).
- [ ] The shipped **reference report** matches the current workbook, extraction config, and
  sweep (it is not stale) (§2.4).

#### Pre-flight, coverage, and per-tool map (§4–§7)

- [ ] All **three pre-flight checks** run before any scenario: path/package verification,
  staleness (content-hash **hard-fail**), and binding-cell verification (§4).
- [ ] Sheet-qualified addresses are normalized with `normalize_key` / `parse_address` at harness
  boundaries before graph lookup or xlwings writes — not compared or split with naive
  `split("!", 1)` (Configure address-keys note).
- [ ] Enum public input writes use **workbook-exact reference label strings** — logical scenario
  values are resolved at the harness boundary, not trimmed or canonicalized on write
  (Configure label-resolution note).
- [ ] The sweep includes the canonical, single-axis, full-factorial, and boundary groups (§5).
- [ ] The sweep includes an **error/boundary group** that actually *exercises* error-class
  equality — reachable error-return paths and domain edges, not happy-path only (§5).
- [ ] The binding map, scenario axes, and output cell set are the **tool's own**; for DDT the
  valid categorical/classification values are enumerated (handled by ladder rule 3, no new
  comparison rule) (§6/§7).

#### Limitations designed against (§8)

- [ ] The Python/Excel **environment is recorded** so parity is reproducible at the last bits
  (§8).
