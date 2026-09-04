# Lessons Learned

## Bottom Line

The core bet held: a small but representative Excel workbook can be turned into a semantic, distributable Python library when we combine target-driven graph extraction, explicit dynamic-reference constraints, series bindings, LLM-assisted naming and documentation, Excel-backed **graph-oracle** parity, and FormulaEvaluator **library** parity. Live export is inverted-tree: keyword-only `compute_*`, no `make_context` / `set_*`. Annotate writes docstrings; validate oracles the graph, not Excel. This can be productionized as a software assembly line, and the strategy *should* scale to larger workbooks such as Q-CRAFT and DDT, though that's yet to be proven.

## What The Harness Caught

Graph-vs-Excel caught extraction, constraint, and binding defects before codegen. Library-vs-graph (`FormulaEvaluator` vs keyword-only `compute_*`) then localizes codegen bugs without a second Excel COM path. The older ctx refactor harness used to catch bad internals rewrites (mostly context-passing mistakes, not model-authored math); that Pass-1 / Pass-2 path is dormant until [#55](https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline/issues/55).

## What It Did Not Catch

The differential harness cannot prove that graph extraction is correct across all plausible workbook states; it only catches extraction defects that surface as parity failures in the tested scenarios. We supplemented it with an opt-in LLM graph spot-check, but that check is also non-exhaustive and does not guarantee coverage of every conditional dynamic-reference path. We may want to harden the pipeline to guarantee that we've correctness-tested every branch.

The harness also cannot prove economic adequacy, correct pipeline configuration, or good library design. It does not validate whether the public API is usable and economist-friendly, or whether docstrings and guide prose are sufficient. These things are all out of scope for differential testing and must be validated by LLM-powered evals or manual human review.

Library ≈ Excel is a **transitivity** claim: it holds only on the scenarios where both graph-vs-Excel and library-vs-graph passed. The library harness does not open Excel.

## What OFFSET / INDEX-MATCH Still Hides

Tiny DSA intentionally exercised the dangerous patterns: `OFFSET`, `INDEX/MATCH`, `CHOOSE`, cross-sheet references, and time recursion. The rehearsal fixed several parser and range-expansion bugs, but the hard problem remains bigger than the toy. Dynamic references only become safe when their controlling cells are constrained with domain knowledge. If constraints are too narrow, the graph can omit real branches that users might want to exercise. If constraints are too wide, resolution can become slow or combinatorial. Nested dynamic refs and large lookup tables remain the place where `excel-grapher` can hide missing dependencies or performance cliffs until a larger workbook forces them into view. There remains important development work to do on `excel-grapher` to make dynamic reference resolution faster and more robust. Tackle this incrementally during DDT/Q-CRAFT extraction as it becomes an issue.

## What Generalizes

The reusable pattern:

- Build a workbook-specific input bundle: workbook, human guide, targets, bindings, constraints, golden scenarios, and artifact catalog.
- Use target-driven graph extraction and keep graph provenance on from the start.
- Separate public API semantics from workbook coordinates through series bindings.
- Preserve a mechanical trace from generated Python back to workbook cells.
- Treat LLM output as draft docs/config, then validate it with deterministic checks and parity.
- Prove extraction with Excel vs `FormulaEvaluator`, then prove codegen with `FormulaEvaluator` vs `compute_*`. Do not drive Excel from inverted-tree `compute_*`.

Shared `compute_*` names across output shards are a deliberate merge signal for complementary slices of one logical public series (for example Gap milestone columns). The same pattern is wrong for distinct scenario or engine-sheet shards: export merges the colliding definitions and most paths become unreachable even though every shard still looks valid in YAML. Uniquify public names per path unless a merge is intentional; see [bindings/README.md](bindings/README.md).

The cookie-cutter for tool #3 should make these defaults hard to skip. The best automation is "make the next missing decision visible."

## Approach For DDT/Q-CRAFT v2

For DDT/Q-CRAFT v2, clone the `src/` and `tests/`, populate `data/` with new workbook and guidance note, but delete `bindings/*.bindings.yaml`, `dist/` contents, and `.cache/`, and change configuration variables in the pipeline to point to the new workbook. With a coding agent, analyze the new workbook, declare targets, author the input and output bindings, constrain dynamic refs, and audit generalizability of tests to the new case. Then run the pipeline to extract with provenance, export keyword-only `compute_*`, annotate docstrings, and validate against `FormulaEvaluator`. Keep graph-vs-Excel as the extraction proof (Windows). Finally, run the authored library-vs-graph sweep. Work to make incremental progress on the "Known gaps/footguns" from [technical_standard.md](technical_standard.md) as we go.
