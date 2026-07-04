"""Tests for concurrent semantic labeling with async-safe cache access."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.grapher.node import Node, NodeKey
from openai import AsyncOpenAI

from src.llm_providers import (
    LLM_MAX_CONCURRENT_ENV,
    ProviderConfig,
    reset_llm_semaphore,
)
from src.semantic_labeling import (
    CellSemanticLabels,
    SemanticLabel,
    SheetSemanticLabels,
    get_sheet_semantic_labels_async,
    label_internal_graph_cells,
    load_json_cache,
)


class _TrackingAsyncCompletions:
    def __init__(self, *, delay: float = 0.05) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.call_count = 0
        self._delay = delay

    async def create(self, **kwargs: object) -> object:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.call_count += 1
        try:
            await asyncio.sleep(self._delay)
            sheet_name = _sheet_name_from_messages(
                cast(list[dict[str, str]], kwargs["messages"])
            )
            payload = _sheet_response(sheet_name)
            return _FakeResponse(json.dumps(payload))
        finally:
            self.in_flight -= 1


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeAsyncChat:
    def __init__(self, completions: _TrackingAsyncCompletions) -> None:
        self.completions = completions


class FakeAsyncClient:
    def __init__(self, completions: _TrackingAsyncCompletions) -> None:
        self.chat = _FakeAsyncChat(completions)


FAKE_PROVIDER = ProviderConfig(
    name="fake",
    api_key_env="FAKE_API_KEY",
    base_url="https://example.invalid/",
    supports_structured_outputs=False,
    supports_reasoning_effort=True,
)


def _sheet_name_from_messages(messages: list[dict[str, str]]) -> str:
    user_content = messages[-1]["content"]
    for line in user_content.splitlines():
        if line.startswith("Worksheet:"):
            return line.split(":", 1)[1].strip()
    raise ValueError(f"worksheet name not found in prompt: {user_content[:120]!r}")


def _sheet_response(sheet_name: str) -> dict[str, object]:
    return {
        "cells": [
            {
                "address": f"{sheet_name}!A1",
                "table_labels": [{"label": f"{sheet_name} table", "concept": None}],
                "row_labels": [],
                "column_labels": [],
            }
        ]
    }


def _minimal_graph() -> DependencyGraph:
    graph = DependencyGraph()
    graph.add_node(
        Node(
            sheet="Alpha",
            column="A",
            row=1,
            formula=None,
            normalized_formula=None,
            value=1,
            is_leaf=True,
            metadata={},
        )
    )
    graph.add_node(
        Node(
            sheet="Beta",
            column="A",
            row=1,
            formula=None,
            normalized_formula=None,
            value=2,
            is_leaf=True,
            metadata={},
        )
    )
    return graph


def _minimal_concept_scheme() -> dict[str, object]:
    return {"concepts": [{"id": "TABLE", "label": "Table"}]}


@pytest.fixture(autouse=True)
def _reset_semaphore() -> Iterator[None]:
    reset_llm_semaphore()
    yield
    reset_llm_semaphore()


def test_get_sheet_semantic_labels_async_respects_semaphore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(LLM_MAX_CONCURRENT_ENV, "1")
    reset_llm_semaphore()
    completions = _TrackingAsyncCompletions(delay=0.05)
    fake_client = cast(AsyncOpenAI, FakeAsyncClient(completions))
    cache_path = tmp_path / "semantic-labels.json"
    concept_scheme = _minimal_concept_scheme()
    sheet_cells = [
        {
            "address": "Alpha!A1",
            "value": 1,
            "formula": None,
            "is_graph_cell": True,
            "needs_semantic_labels": True,
        }
    ]

    async def run() -> None:
        await asyncio.gather(
            get_sheet_semantic_labels_async(
                sheet_name="Alpha",
                candidate_addresses=["Alpha!A1"],
                sheet_cells=sheet_cells,
                concept_scheme=concept_scheme,
                model="gpt-5.5",
                cache_path=cache_path,
                client=fake_client,
                provider=FAKE_PROVIDER,
            ),
            get_sheet_semantic_labels_async(
                sheet_name="Beta",
                candidate_addresses=["Beta!A1"],
                sheet_cells=[
                    {
                        "address": "Beta!A1",
                        "value": 2,
                        "formula": None,
                        "is_graph_cell": True,
                        "needs_semantic_labels": True,
                    }
                ],
                concept_scheme=concept_scheme,
                model="gpt-5.5",
                cache_path=cache_path,
                client=fake_client,
                provider=FAKE_PROVIDER,
            ),
        )

    with patch(
        "src.semantic_labeling.build_async_client_if_configured",
        return_value=(fake_client, FAKE_PROVIDER),
    ):
        asyncio.run(run())

    assert completions.max_in_flight <= 1
    assert completions.call_count == 2
    cache = load_json_cache(cache_path)
    assert len(cache) == 2


def test_label_internal_graph_cells_is_deterministic_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(LLM_MAX_CONCURRENT_ENV, "3")
    reset_llm_semaphore()

    def fake_labels(
        sheet_name: str,
        candidate_addresses: list[NodeKey],
        _sheet_cells: list[dict[str, Any]],
        _concept_scheme: Mapping[str, Any],
    ) -> SheetSemanticLabels:
        return SheetSemanticLabels(
            cells=[
                CellSemanticLabels(
                    address=address,
                    table_labels=[
                        SemanticLabel(label=f"{sheet_name}-{address}", concept=None)
                    ],
                )
                for address in candidate_addresses
            ]
        )

    graph_a = _minimal_graph()
    graph_b = _minimal_graph()
    workbook_path = tmp_path / "workbook.xlsx"

    import fastpyxl

    workbook = fastpyxl.Workbook()
    workbook.active.title = "Alpha"
    workbook.create_sheet("Beta")
    workbook.save(workbook_path)

    summary_a = label_internal_graph_cells(
        graph=graph_a,
        workbook_path=workbook_path,
        input_cells=[],
        target_cells=[],
        concept_scheme=_minimal_concept_scheme(),
        provider=fake_labels,
        cache_path=tmp_path / "cache-a.json",
    )
    summary_b = label_internal_graph_cells(
        graph=graph_b,
        workbook_path=workbook_path,
        input_cells=[],
        target_cells=[],
        concept_scheme=_minimal_concept_scheme(),
        provider=fake_labels,
        cache_path=tmp_path / "cache-b.json",
    )

    assert summary_a.labeled_cell_count == summary_b.labeled_cell_count
    alpha_a = graph_a.get_node("Alpha!A1")
    alpha_b = graph_b.get_node("Alpha!A1")
    beta_a = graph_a.get_node("Beta!A1")
    beta_b = graph_b.get_node("Beta!A1")
    assert alpha_a is not None and alpha_b is not None
    assert beta_a is not None and beta_b is not None
    assert alpha_a.metadata == alpha_b.metadata
    assert beta_a.metadata == beta_b.metadata
