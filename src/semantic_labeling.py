from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fastpyxl
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import NodeKey
from fastpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict, Field

from src.llm_json import generate_validated_json
from src.llm_providers import build_client

SEMANTIC_LABEL_MODEL_ENV = "SEMANTIC_LABEL_MODEL"
DEFAULT_SEMANTIC_LABEL_PROMPT_VERSION = 1
DEFAULT_SEMANTIC_LABEL_CACHE_PATH = (
    Path(__file__).resolve().parents[1] / ".cache/semantic-labels.json"
)

SheetLabelProvider = Callable[
    [str, list[NodeKey], list[dict[str, Any]], Mapping[str, Any]],
    "SheetSemanticLabels",
]


def resolve_semantic_label_model(model: str | None) -> str:
    """Return the caller's model, else the ``SEMANTIC_LABEL_MODEL`` env value.

    Model switching is configured via the environment (loaded from ``.env``),
    so an explicit ``None`` with no configured variable is a hard error rather
    than a silent fallback to some default provider.
    """
    if model is not None:
        return model
    env_model = os.environ.get(SEMANTIC_LABEL_MODEL_ENV)
    if not env_model:
        raise RuntimeError(
            f"{SEMANTIC_LABEL_MODEL_ENV} must be set (e.g. in .env) or a model "
            "passed explicitly to select the semantic labeling provider"
        )
    return env_model


class SemanticLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Visible label text from the worksheet.")
    concept: str | None = Field(
        default=None,
        description=(
            "Concept ID from the supplied concept scheme when the label represents "
            "one of those concepts; otherwise null."
        ),
    )
    source_address: str | None = Field(
        default=None,
        description="Sheet-qualified address of the cell containing this label.",
    )


class CellSemanticLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: NodeKey = Field(description="Sheet-qualified graph cell address.")
    table_labels: list[SemanticLabel] = Field(default_factory=list)
    row_labels: list[SemanticLabel] = Field(default_factory=list)
    column_labels: list[SemanticLabel] = Field(default_factory=list)


class SheetSemanticLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cells: list[CellSemanticLabels] = Field(
        description="One entry for every requested graph cell on the worksheet."
    )


@dataclass(frozen=True)
class SemanticLabelingSummary:
    labeled_cell_count: int
    sheet_count: int
    candidate_cells_by_sheet: dict[str, list[NodeKey]]


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sheet_name_for_key(graph: DependencyGraph, key: NodeKey) -> str:
    node = graph.get_node(key)
    if node is not None and node.sheet:
        return node.sheet
    return key.split("!", 1)[0]


def group_candidate_cells_by_sheet(
    graph: DependencyGraph,
    *,
    input_cells: Iterable[NodeKey],
    target_cells: Iterable[NodeKey],
) -> dict[str, list[NodeKey]]:
    excluded_cells = set(input_cells) | set(target_cells)
    candidate_cells_by_sheet: dict[str, list[NodeKey]] = defaultdict(list)
    for key in graph.keys(order="workbook"):
        if key in excluded_cells:
            continue
        candidate_cells_by_sheet[sheet_name_for_key(graph, key)].append(key)
    return dict(candidate_cells_by_sheet)


def sheet_cells_for_prompt(
    formula_workbook: Any,
    value_workbook: Any,
    *,
    sheet_name: str,
    candidate_addresses: set[NodeKey],
    graph_addresses: set[NodeKey],
) -> list[dict[str, Any]]:
    formula_ws = formula_workbook[sheet_name]
    value_ws = value_workbook[sheet_name]
    cells: list[dict[str, Any]] = []
    for row_idx in range(1, formula_ws.max_row + 1):
        for col_idx in range(1, formula_ws.max_column + 1):
            column = get_column_letter(col_idx)
            address = f"{sheet_name}!{column}{row_idx}"
            formula_value = formula_ws.cell(row=row_idx, column=col_idx).value
            cached_value = value_ws.cell(row=row_idx, column=col_idx).value
            if (
                formula_value is None
                and cached_value is None
                and address not in graph_addresses
            ):
                continue
            formula = formula_value if str(formula_value).startswith("=") else None
            cells.append(
                {
                    "address": address,
                    "value": cached_value,
                    "formula": formula,
                    "is_graph_cell": address in graph_addresses,
                    "needs_semantic_labels": address in candidate_addresses,
                }
            )
    return cells


def prompt_for_sheet_semantic_labels(
    *,
    sheet_name: str,
    candidate_addresses: list[NodeKey],
    sheet_cells: list[dict[str, Any]],
    concept_scheme: Mapping[str, Any],
    response_schema: Mapping[str, Any],
) -> str:
    return f"""
You are labeling internal Excel dependency graph cells for refactoring.

Return exactly one `cells` entry for each requested candidate address. Do not
return input cells, output/target cells, or cells that are not in the candidate
address list. Labels should describe the workbook context visible on the sheet:

- `table_labels`: section or table titles that apply to the cell.
- `row_labels`: left-side labels or row headers that identify the row.
- `column_labels`: top headers or other column labels that identify the column.

Use the supplied concept scheme for `concept` whenever a label represents a
known concept. For example, projection years or year offsets should use
`TIME_PERIOD`; country names should use `COUNTRY`; scenario variable names
should usually use `INDICATOR`, `PARAMETER`, or `SHOCK_PARAMETER` depending on
the sheet context. Use null when no supplied concept applies. Set
`source_address` to the sheet-qualified cell address where the visible label
appears, when known.

Worksheet: {sheet_name}

Candidate addresses:
{json.dumps(candidate_addresses, indent=2)}

Concept scheme:
{json.dumps(concept_scheme, indent=2)}

Worksheet cells:
{json.dumps(sheet_cells, indent=2, default=str)}

Response schema:
{json.dumps(response_schema, indent=2)}
""".strip()


def semantic_label_cache_key(
    *,
    sheet_name: str,
    candidate_addresses: list[NodeKey],
    sheet_cells: list[dict[str, Any]],
    concept_scheme: Mapping[str, Any],
    response_schema: Mapping[str, Any],
    model: str,
    prompt_version: int,
) -> str:
    payload = {
        "model": model,
        "prompt_version": prompt_version,
        "sheet_name": sheet_name,
        "candidate_addresses": candidate_addresses,
        "sheet_cells": sheet_cells,
        "concept_scheme": concept_scheme,
        "response_schema": response_schema,
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def load_json_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.exists():
        return {}
    return json.loads(cache_path.read_text(encoding="utf-8"))


def save_json_cache(cache_path: Path, cache: Mapping[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def valid_concepts_from_scheme(concept_scheme: Mapping[str, Any]) -> set[str]:
    concepts = concept_scheme.get("concepts")
    if not isinstance(concepts, list):
        raise TypeError("concept_scheme must contain a concepts list")
    return {
        concept["id"]
        for concept in concepts
        if isinstance(concept, Mapping) and isinstance(concept.get("id"), str)
    }


def validate_sheet_semantic_labels(
    result: SheetSemanticLabels,
    *,
    candidate_addresses: set[NodeKey],
    valid_concepts: set[str],
) -> None:
    returned_addresses = {cell.address for cell in result.cells}
    missing = sorted(candidate_addresses - returned_addresses)
    unexpected = sorted(returned_addresses - candidate_addresses)
    if missing or unexpected:
        raise RuntimeError(
            "Semantic labeling response did not match requested cells: "
            f"missing={missing}, unexpected={unexpected}"
        )
    for cell in result.cells:
        for label_group in (
            cell.table_labels,
            cell.row_labels,
            cell.column_labels,
        ):
            for label in label_group:
                if label.concept is not None and label.concept not in valid_concepts:
                    raise RuntimeError(
                        f"Unknown concept {label.concept!r} for {cell.address}"
                    )


def get_sheet_semantic_labels(
    *,
    sheet_name: str,
    candidate_addresses: list[NodeKey],
    sheet_cells: list[dict[str, Any]],
    concept_scheme: Mapping[str, Any],
    model: str | None = None,
    prompt_version: int = DEFAULT_SEMANTIC_LABEL_PROMPT_VERSION,
    cache_path: Path = DEFAULT_SEMANTIC_LABEL_CACHE_PATH,
    api_key: str | None = None,
) -> SheetSemanticLabels:
    model = resolve_semantic_label_model(model)
    schema = SheetSemanticLabels.model_json_schema()
    cache = load_json_cache(cache_path)
    cache_key = semantic_label_cache_key(
        sheet_name=sheet_name,
        candidate_addresses=candidate_addresses,
        sheet_cells=sheet_cells,
        concept_scheme=concept_scheme,
        response_schema=schema,
        model=model,
        prompt_version=prompt_version,
    )
    if cache_key in cache:
        content = cache[cache_key]
    else:
        client, provider = build_client(model, api_key=api_key)
        _, content = generate_validated_json(
            client=client,
            model=model,
            provider=provider,
            system_prompt=(
                "You label Excel workbook cells for dependency graph "
                "refactoring. Return only valid JSON matching the schema."
            ),
            user_prompt=prompt_for_sheet_semantic_labels(
                sheet_name=sheet_name,
                candidate_addresses=candidate_addresses,
                sheet_cells=sheet_cells,
                concept_scheme=concept_scheme,
                response_schema=schema,
            ),
            response_model=SheetSemanticLabels,
        )
        cache[cache_key] = content
        save_json_cache(cache_path, cache)
    return SheetSemanticLabels.model_validate_json(content)


def set_cell_semantic_metadata(
    graph: DependencyGraph,
    cell_labels: CellSemanticLabels,
) -> None:
    node = graph.get_node(cell_labels.address)
    existing_metadata = dict(node.metadata) if node is not None else {}
    label_metadata = cell_labels.model_dump(exclude={"address"}, exclude_none=True)
    graph.set_node_metadata(
        cell_labels.address,
        existing_metadata | label_metadata,
    )


def label_internal_graph_cells(
    *,
    graph: DependencyGraph,
    workbook_path: Path,
    input_cells: Iterable[NodeKey],
    target_cells: Iterable[NodeKey],
    concept_scheme: Mapping[str, Any],
    provider: SheetLabelProvider | None = None,
    model: str | None = None,
    prompt_version: int = DEFAULT_SEMANTIC_LABEL_PROMPT_VERSION,
    cache_path: Path = DEFAULT_SEMANTIC_LABEL_CACHE_PATH,
    api_key: str | None = None,
) -> SemanticLabelingSummary:
    formula_workbook = fastpyxl.load_workbook(workbook_path, data_only=False)
    value_workbook = fastpyxl.load_workbook(workbook_path, data_only=True)
    graph_cells = set(graph.keys(order="workbook"))
    candidate_cells_by_sheet = group_candidate_cells_by_sheet(
        graph,
        input_cells=input_cells,
        target_cells=set(target_cells) | set(graph.target_keys()),
    )
    valid_concepts = valid_concepts_from_scheme(concept_scheme)

    for sheet_name, candidate_addresses in sorted(candidate_cells_by_sheet.items()):
        sheet_candidate_set = set(candidate_addresses)
        sheet_cells = sheet_cells_for_prompt(
            formula_workbook,
            value_workbook,
            sheet_name=sheet_name,
            candidate_addresses=sheet_candidate_set,
            graph_addresses=graph_cells,
        )
        if provider is None:
            sheet_labels = get_sheet_semantic_labels(
                sheet_name=sheet_name,
                candidate_addresses=candidate_addresses,
                sheet_cells=sheet_cells,
                concept_scheme=concept_scheme,
                model=model,
                prompt_version=prompt_version,
                cache_path=cache_path,
                api_key=api_key,
            )
        else:
            sheet_labels = provider(
                sheet_name,
                candidate_addresses,
                sheet_cells,
                concept_scheme,
            )
        validate_sheet_semantic_labels(
            sheet_labels,
            candidate_addresses=sheet_candidate_set,
            valid_concepts=valid_concepts,
        )
        for cell_labels in sheet_labels.cells:
            set_cell_semantic_metadata(graph, cell_labels)

    return SemanticLabelingSummary(
        labeled_cell_count=sum(
            len(cells) for cells in candidate_cells_by_sheet.values()
        ),
        sheet_count=len(candidate_cells_by_sheet),
        candidate_cells_by_sheet=candidate_cells_by_sheet,
    )
