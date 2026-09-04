"""Tests for the Tiny-DSA workbook builder."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPO_ROOT / "data" / "build_tiny_dsa.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_tiny_dsa", BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load builder from {BUILDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_tiny_dsa_does_not_write_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _load_builder()
    out_xlsx = tmp_path / "tiny-dsa.xlsx"
    out_manifest = tmp_path / "tiny-dsa-manifest.json"
    monkeypatch.setattr(builder, "HERE", tmp_path)
    monkeypatch.setattr(builder, "OUT_XLSX", out_xlsx)
    builder.main()
    assert out_xlsx.is_file()
    assert not out_manifest.exists()
