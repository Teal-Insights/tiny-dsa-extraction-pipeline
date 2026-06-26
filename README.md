## Running the pipeline

The pipeline reverse-engineers the illustrative Excel workbook at [`data/tiny-dsa.xlsx`](data/tiny-dsa.xlsx) and exports a standalone Python package (plus its documentation site sources) into `dist/`. It runs in three stages:

1. **configure**: declare input/output series bindings, classify graph leaves as inputs or constants, and constrain input cell domains
2. **extract and export**: trace the target cells' dependencies, build the dependency graph, and generate Python code
3. **refactor**: cluster and rewrite the generated `internals.py`, then build the Great Docs user-guide sources

All three stages are driven by the single entry point in [`src/extraction_pipeline.py`](src/extraction_pipeline.py).

### Prerequisites

First install the dependencies (see [Setup](#setup)):

```bash
uv sync
```

The pipeline can call the OpenAI API for the steps that use an LLM (semantic cell labeling, docstring generation, `internals.py` refactoring, and user-guide rewrites). Cached results for these steps are committed to the repository (under `.cache/`), so a clean run reproduces the current output **without** an API key. You only need a key if you change inputs in a way that invalidates the cache; in that case, provide it via a `.env` file at the repository root:

```bash
# .env
OPENAI_API_KEY=sk-...
```

If an uncached LLM step is reached without a key, the pipeline fails fast with a `OPENAI_API_KEY is required ...` error.

### Run

```bash
uv run python -m src.extraction_pipeline
```

This exports the generated `tiny_dsa` package and supporting assets into `dist/`, rewrites the clustered `internals.py` in place, and generates the documentation pipeline output (user-guide pages, `great-docs.yml`, and the docs deploy workflow). On every push to `main`, [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) runs this same command and syncs `dist/` to the [`Teal-Insights/py-tiny-dsa`](https://github.com/Teal-Insights/py-tiny-dsa) repository.

## Dependency graph

The pipeline for extracting the dependency graph lives in the [extraction-pipeline.qmd](docs/extraction-pipeline.qmd) file.

This file renders to [extraction-pipeline.md](docs/extraction-pipeline.md), where you can see the dependency graph as a Mermaid diagram.

## LLM-based Correctness Testing

The `pytest` test suite includes an opt-in test that uses the OpenAI API on high-thinking mode to judge the correctness of the extracted dependency graph. To run this test, pass the `--run-skipped` flag to `pytest`.

## Differential Testing

A differential test sweep assesses whether recomputing the output cells of the extracted formula graph with `FormulaEvaluator.evaluate()` for a range of different inputs gives the same results as driving the original workbook with xlwings and Microsoft Excel. For instructions on running the differential test sweep, see the [differential testing README](tests/differential/README.md). The test sweep passes cleanly with the current pipeline. Test results may be viewed in the [differential/differential_report.txt](data/differential/differential_report.txt) file.

The `pytest` test suite also runs a small sample of differential tests with randomized inputs.

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