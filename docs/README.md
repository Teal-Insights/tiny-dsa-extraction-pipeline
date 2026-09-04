# Docs

Authored notes:

- [extraction-pipeline.md](extraction-pipeline.md) — historical Tiny DSA extraction walkthrough. Treat schema versions and artifact paths in that file as possibly stale relative to the current orchestrator.
- [inverted-tree-migration.md](inverted-tree-migration.md) — how a derived extraction-pipeline repo moves from ctx export to inverted-tree `compute_*`, FormulaEvaluator validate, and graph-oracle library differentials.

This directory also holds **generated** exploration artifacts for the distributable docs site. Do not commit graph JSON/HTML snapshots.

Dependency graph review artifacts now live under [`artifacts/dependency-graph/`](../artifacts/dependency-graph/). See [artifacts/artifacts-catalog.md](../artifacts/artifacts-catalog.md) for the extract stage and summary schema.

The archived literate notebook that informed the README workflow lives in [archive/extraction-pipeline.qmd](../archive/extraction-pipeline.qmd). That notebook is historical only; do not treat its schema versions or artifact paths as current.
