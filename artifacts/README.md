# Artifacts

Generated pipeline artifacts live under `artifacts/`. See [artifacts-catalog.md](artifacts-catalog.md) for file schemas and local preview commands.

## What to commit

| Path | Git policy |
|---|---|
| `artifacts/dependency-graph/` | **Gitignored** — regenerate with `--extract-graph`; do not commit HTML/JSON snapshots |
| `artifacts/stages/` | **Gitignored** — local stage manifests for `--start-from-stage` / `--only-stage` |
| `artifacts/stage-timings.json` | **Gitignored** — per-run timing diagnostics |
| `artifacts/workbook-audit.md` | **Optional** — commit when you want a dated audit record in version control; otherwise regenerate locally |

The dependency-graph directory is listed in `.gitignore` because graph payloads are large and workbook-specific. Workbook audit reports are small markdown summaries and may be checked in for team review when useful.

## When each artifact is produced

| Stage | Command | Output |
|---|---|---|
| Audit | `uv run python -m src.workbook_audit --output artifacts/workbook-audit.md` | `workbook-audit.md` |
| Extract | Full pipeline, or `uv run python -m src.extraction_pipeline --extract-graph` | `dependency-graph/…`, `stages/extract.json` |
| Export+ | `uv run python -m src.extraction_pipeline [--stop-after-stage …]` | `stages/{export,refactor,validate,document}.json` as stages complete; a default full run also runs extract first |

Serve the graph explorer locally (do not commit generated files):

```bash
uv run python -m http.server 8000 --directory artifacts/dependency-graph
```

Open `http://localhost:8000/`.
