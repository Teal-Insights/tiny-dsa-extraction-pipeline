## Dependency graph

The pipeline for extracting the dependency graph lives in the [extraction-pipeline.qmd](extraction-pipeline.qmd) file.

This file renders to [extraction-pipeline.md](extraction-pipeline.md), where you can see the dependency graph as a Mermaid diagram.

## Development

### Setup

Clone the repository and install the dependencies:

```bash
git clone https://github.com/Teal-Insights/tiny-dsa-extraction-pipeline.git
cd tiny-dsa-extraction-pipeline
uv sync
uv run pre-commit install
```

### Workflow

Type, lint, and format checks are run automatically when you commit.

Lint:

```bash
uv run ruff check
```

Format:

```bash
uv run ruff format
```

Type check:

```bash
uv run ty check
```