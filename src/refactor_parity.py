from __future__ import annotations

import importlib
import math
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.internals_refactor import (
        ClusterRefactorContext,
        ClusterRefactorResponse,
        SingletonRefactorContext,
        SingletonRefactorResponse,
    )

REFACTOR_PARITY_ATOL = 1e-9


@dataclass(frozen=True)
class RefactorParityError(ValueError):
    kind: str
    label: str
    address: str
    expected: Any
    actual: Any

    def __str__(self) -> str:
        return (
            f"Refactor parity failed for {self.kind} {self.label!r} at "
            f"{self.address}: expected {self.expected!r}, got {self.actual!r}"
        )


def _package_root(internals_path: Path) -> Path:
    return internals_path.parent.parent


def _cell_values_close(
    expected: Any, actual: Any, *, atol: float = REFACTOR_PARITY_ATOL
) -> bool:
    if type(expected) is type(actual) and hasattr(expected, "name"):
        return expected == actual

    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False

    if isinstance(expected, bool) and isinstance(actual, bool):
        return expected == actual

    if isinstance(expected, int | float) and isinstance(actual, int | float):
        expected_float = float(expected)
        actual_float = float(actual)
        if math.isnan(expected_float) and math.isnan(actual_float):
            return True
        return abs(expected_float - actual_float) <= atol

    return expected == actual


def evaluate_internals_addresses(
    *,
    package_root: Path,
    internals_source: str,
    addresses: tuple[str, ...],
) -> dict[str, Any]:
    """Evaluate workbook addresses from a scratch copy of ``internals.py``."""
    from src.internals_refactor import validate_refactored_internals

    validate_refactored_internals(internals_source)

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        shutil.copytree(package_root / "tiny_dsa", temp_root / "tiny_dsa")
        (temp_root / "tiny_dsa" / "internals.py").write_text(
            internals_source,
            encoding="utf-8",
        )
        return _import_and_evaluate(temp_root, addresses)


def _import_and_evaluate(
    package_root: Path, addresses: tuple[str, ...]
) -> dict[str, Any]:
    root_str = str(package_root.resolve())
    inserted = root_str not in sys.path
    if inserted:
        sys.path.insert(0, root_str)

    for module_name in list(sys.modules):
        if module_name == "tiny_dsa" or module_name.startswith("tiny_dsa."):
            del sys.modules[module_name]

    try:
        runtime = importlib.import_module("tiny_dsa.runtime")
        api = importlib.import_module("tiny_dsa.api")
        ctx = api.make_context()
        return {address: runtime.xl_cell(ctx, address) for address in addresses}
    finally:
        if inserted:
            sys.path.remove(root_str)
        for module_name in list(sys.modules):
            if module_name == "tiny_dsa" or module_name.startswith("tiny_dsa."):
                del sys.modules[module_name]


def validate_cluster_refactor_parity(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
    *,
    internals_path: Path,
) -> None:
    from src.internals_refactor import apply_refactor_plan

    source = internals_path.read_text(encoding="utf-8")
    candidate = apply_refactor_plan(source, response, ctx, phase_b=False)
    addresses = tuple(member.address for member in ctx.members)
    package_root = _package_root(internals_path)

    expected = evaluate_internals_addresses(
        package_root=package_root,
        internals_source=source,
        addresses=addresses,
    )
    actual = _evaluate_cluster_candidate(
        package_root=package_root,
        internals_source=candidate,
        response=response,
    )

    for address in addresses:
        if not _cell_values_close(expected[address], actual[address]):
            raise RefactorParityError(
                kind="cluster",
                label=f"row {ctx.row} ({response.helper_name})",
                address=address,
                expected=expected[address],
                actual=actual[address],
            )


def _evaluate_cluster_candidate(
    *,
    package_root: Path,
    internals_source: str,
    response: ClusterRefactorResponse,
) -> dict[str, Any]:
    from src.internals_refactor import validate_refactored_internals

    validate_refactored_internals(internals_source)

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        shutil.copytree(package_root / "tiny_dsa", temp_root / "tiny_dsa")
        (temp_root / "tiny_dsa" / "internals.py").write_text(
            internals_source,
            encoding="utf-8",
        )
        return _import_and_evaluate_cluster_candidate(temp_root, response)


def _import_and_evaluate_cluster_candidate(
    package_root: Path,
    response: ClusterRefactorResponse,
) -> dict[str, Any]:
    root_str = str(package_root.resolve())
    inserted = root_str not in sys.path
    if inserted:
        sys.path.insert(0, root_str)

    for module_name in list(sys.modules):
        if module_name == "tiny_dsa" or module_name.startswith("tiny_dsa."):
            del sys.modules[module_name]

    try:
        api = importlib.import_module("tiny_dsa.api")
        internals = importlib.import_module("tiny_dsa.internals")
        ctx = api.make_context()
        helper = getattr(internals, response.helper_name)
        values: dict[str, Any] = {}
        for entry in response.member_keys:
            kwargs = {
                parameter.name: entry.keys[parameter.concept]
                for parameter in response.parameters
            }
            values[entry.address] = helper(ctx, **kwargs)
        return values
    finally:
        if inserted:
            sys.path.remove(root_str)
        for module_name in list(sys.modules):
            if module_name == "tiny_dsa" or module_name.startswith("tiny_dsa."):
                del sys.modules[module_name]


def validate_singleton_refactor_parity(
    ctx: SingletonRefactorContext,
    response: SingletonRefactorResponse,
    *,
    internals_path: Path,
) -> None:
    from src.internals_refactor import apply_singleton_refactor_plan

    source = internals_path.read_text(encoding="utf-8")
    candidate, _rewrite_count = apply_singleton_refactor_plan(source, response, ctx)
    address = ctx.address
    package_root = _package_root(internals_path)

    expected = evaluate_internals_addresses(
        package_root=package_root,
        internals_source=source,
        addresses=(address,),
    )
    actual = evaluate_internals_addresses(
        package_root=package_root,
        internals_source=candidate,
        addresses=(address,),
    )

    if not _cell_values_close(expected[address], actual[address]):
        raise RefactorParityError(
            kind="singleton",
            label=f"{ctx.symbol_name} ({address})",
            address=address,
            expected=expected[address],
            actual=actual[address],
        )
