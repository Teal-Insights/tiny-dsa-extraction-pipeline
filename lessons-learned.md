# Lessons Learned

## Bottom Line

The core bet held: a small but representative Excel workbook can be turned into a semantic, distributable Python library when we combine target-driven graph extraction, explicit dynamic-reference constraints, series bindings, LLM-assisted naming/refactoring/docs, and Excel-backed parity tests. This can be productionized as a software assembly line, and the strategy *should* scale to larger workbooks such as Q-CRAFT and DDT, though that's yet to be proven.

## What The Harness Caught

The harness successfully caught bad refactors of Excel internals. The mistakes turned out to be mostly due to errors in our context-passing rather than AI code-writing mistakes. We were able to strengthen prompts and context-building, implement retries for our AI refactorer, and get our pipeline consistently producing outputs that pass differential tests.

## What It Did Not Catch

The differential harness cannot prove that graph extraction is correct across all plausible workbook states; it only catches extraction defects that surface as parity failures in the tested scenarios. We supplemented it with an opt-in LLM graph spot-check, but that check is also non-exhaustive and does not guarantee coverage of every conditional dynamic-reference path. We may want to harden the pipeline to guarantee that we've correctness-tested every branch.

The harness also cannot prove economic adequacy, correct pipeline configuration, or good library design. It does not validate whether the public API is usable and economist-friendly, or whether docstrings and guide prose are sufficient. These things are all out of scope for differential testing and must be validated by LLM-powered evals or manual human review.

## What OFFSET / INDEX-MATCH Still Hides

Tiny DSA intentionally exercised the dangerous patterns: `OFFSET`, `INDEX/MATCH`, `CHOOSE`, cross-sheet references, and time recursion. The rehearsal fixed several parser and range-expansion bugs, but the hard problem remains bigger than the toy. Dynamic references only become safe when their controlling cells are constrained with domain knowledge. If constraints are too narrow, the graph can omit real branches that users might want to exercise. If constraints are too wide, resolution can become slow or combinatorial. Nested dynamic refs and large lookup tables remain the place where `excel-grapher` can hide missing dependencies or performance cliffs until a larger workbook forces them into view. There remains important development work to do on `excel-grapher` to make dynamic reference resolution faster and more robust. Tackle this incrementally during DDT/Q-CRAFT extraction as it becomes an issue.

## What Generalizes

The reusable pattern:

- Build a workbook-specific input bundle: workbook, human guide, targets, bindings, constraints, golden scenarios, and artifact catalog.
- Use target-driven graph extraction and keep graph provenance on from the start.
- Separate public API semantics from workbook coordinates through series bindings.
- Preserve a mechanical trace from generated Python back to workbook cells, even after compression/refactor.
- Use non-destructive export projections so refactoring can shrink generated code without destroying canonical graph evidence.
- Treat LLM output as draft code/docs/config, then validate it with deterministic checks and Excel parity.
- Keep LLM refactoring tasks bounded to small subgraphs identified algorithmically, and perform incremental rewriting in reverse topological order from leaf to root. Keep each pass behind a parity gate and retry if it fails correctness testing. This makes the process scalable to workbooks of any size.

Fingerprint formula clusters group cells by AST shape only; they are not always valid atomic refactor nodes. When cross-period lag edges create inter-cluster cycles on an acyclic cell graph, the refactor pipeline schedules **refactor units** (member subsets of fingerprint families) via `compute_refactor_schedule` instead of hard-failing cluster ordering.

Shared `compute_*` / `set_*` names across output/input shards are a deliberate merge signal for complementary slices of one logical public series (for example Gap milestone columns). The same pattern is wrong for distinct scenario or engine-sheet shards: export merges the colliding definitions and most paths become unreachable even though every shard still looks valid in YAML. Uniquify public names per path unless a merge is intentional; see [bindings/README.md](bindings/README.md).

The cookie-cutter for tool #3 should make these defaults hard to skip. The best automation is "make the next missing decision visible."

## Approach For DDT/Q-CRAFT v2

For DDT/Q-CRAFT v2, clone the `src/` and `tests/`, populate `data/` with new workbook and guidance note, but delete `bindings/*.bindings.yaml`, `dist/` contents, and `.cache/`, and change configuration variables in the pipeline to point to the new workbook. With a coding agent, analyze the new workbook, declare targets, author the input and output bindings, constrain dynamic refs, and audit generalizability of tests to the new case. Then run the pipeline to extract with provenance, export a semantic API, run Excel parity, and refactor. Finally, run differential testing to ensure the exported library still passes parity tests. Work to make incremental progress on the "Known gaps/footguns" from [technical_standard.md](technical_standard.md) as we go.