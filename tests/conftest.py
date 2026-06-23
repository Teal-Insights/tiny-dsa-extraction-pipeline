import shutil
import sys
from pathlib import Path

import pytest
from excel_grapher.exporter import CodeGenerator

from src.docstring_callback import available_docstring_callback
from src.extraction_pipeline import (
    graph,
    series_bindings,
    targets,
    workbook_path,
)
from src.formula_clustering import cluster_graph_formulas
from src.internals_refactor import (
    ClusterRefactorResponse,
    REFACTOR_ROW_ORDER,
    SINGLETON_REFACTOR_ORDER,
    apply_phase_b_final_pass,
    apply_phase_c,
    apply_refactor_plan,
    apply_singleton_refactor_plan,
    build_cluster_refactor_context,
    build_singleton_refactor_context,
    validate_refactored_internals,
)
from tests.fixtures.cluster_refactor_golden import GOLDEN_CLUSTER_REFACTOR_RESPONSES
from tests.fixtures.singleton_refactor_golden import GOLDEN_SINGLETON_REFACTOR_RESPONSES
from src.subgraph_projection import build_tiny_dsa_refactor_projection


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-skipped",
        action="store_true",
        default=False,
        help="Run tests marked with @pytest.mark.skipped.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "skipped: marks opt-in tests skipped unless --run-skipped is set",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-skipped"):
        return

    skip_reason = "opt-in test; pass --run-skipped to run"
    for item in items:
        if item.get_closest_marker("skipped"):
            item.add_marker(pytest.mark.skip(reason=skip_reason))


@pytest.fixture(scope="session")
def tiny_dsa_refactor_projection():
    return build_tiny_dsa_refactor_projection(graph)


@pytest.fixture(scope="session")
def codegen_package_root(tmp_path_factory, tiny_dsa_refactor_projection) -> Path:
    root = tmp_path_factory.mktemp("tiny_dsa_codegen")
    package_root = root / "tiny_dsa"
    package_root.mkdir()
    with CodeGenerator(tiny_dsa_refactor_projection) as generator:
        modules = generator.generate_modules(
            targets,
            series_bindings=series_bindings,
            bindings_workbook=workbook_path,
            series_docstring_callback=available_docstring_callback(),
            docstring_renderer="google",
        )
    for filename, code in modules.items():
        output_path = package_root / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(code, encoding="utf-8")
    return root


@pytest.fixture(scope="session")
def codegen_internals_path(codegen_package_root) -> Path:
    return codegen_package_root / "tiny_dsa" / "internals.py"


@pytest.fixture(scope="session")
def codegen_internals_source(codegen_internals_path) -> str:
    return codegen_internals_path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def singleton_refactored_internals_source(
    codegen_internals_source,
    tiny_dsa_refactor_projection,
    codegen_internals_path,
) -> str:
    clusters = cluster_graph_formulas(tiny_dsa_refactor_projection)
    singleton_clusters = {
        cluster.members[0]: cluster for cluster in clusters if len(cluster.members) == 1
    }
    updated = codegen_internals_source
    for address in SINGLETON_REFACTOR_ORDER:
        cluster = singleton_clusters.get(address)
        if cluster is None:
            continue
        ctx = build_singleton_refactor_context(
            tiny_dsa_refactor_projection,
            cluster,
            codegen_internals_path,
        )
        assert ctx is not None
        response = GOLDEN_SINGLETON_REFACTOR_RESPONSES[address]
        updated, _rewrite_count = apply_singleton_refactor_plan(updated, response, ctx)
    validate_refactored_internals(updated)
    codegen_internals_path.write_text(updated, encoding="utf-8")
    return updated


@pytest.fixture(scope="session")
def cluster_refactor_responses(
    tiny_dsa_refactor_projection,
    codegen_internals_path,
) -> tuple[ClusterRefactorResponse, ...]:
    clusters_by_row = {
        cluster.row: cluster
        for cluster in cluster_graph_formulas(tiny_dsa_refactor_projection)
        if cluster.row is not None and len(cluster.members) >= 2
    }
    responses: list[ClusterRefactorResponse] = []
    for row in REFACTOR_ROW_ORDER:
        cluster = clusters_by_row.get(row)
        if cluster is None:
            continue
        ctx = build_cluster_refactor_context(
            tiny_dsa_refactor_projection,
            cluster,
            codegen_internals_path,
        )
        assert ctx is not None
        responses.append(GOLDEN_CLUSTER_REFACTOR_RESPONSES[row])
    return tuple(responses)


@pytest.fixture(scope="session")
def phase_a_internals_source(
    singleton_refactored_internals_source,
    cluster_refactor_responses,
    tiny_dsa_refactor_projection,
    codegen_internals_path,
) -> str:
    clusters_by_row = {
        cluster.row: cluster
        for cluster in cluster_graph_formulas(tiny_dsa_refactor_projection)
        if cluster.row is not None and len(cluster.members) >= 2
    }
    updated = singleton_refactored_internals_source
    response_iter = iter(cluster_refactor_responses)
    for row in REFACTOR_ROW_ORDER:
        cluster = clusters_by_row.get(row)
        if cluster is None:
            continue
        response = next(response_iter)
        ctx = build_cluster_refactor_context(
            tiny_dsa_refactor_projection,
            cluster,
            codegen_internals_path,
        )
        assert ctx is not None
        updated = apply_refactor_plan(updated, response, ctx, phase_b=False)
    validate_refactored_internals(updated)
    return updated


@pytest.fixture(scope="session")
def phase_bc_internals_source(
    phase_a_internals_source,
    cluster_refactor_responses,
) -> str:
    phase_b_source, _phase_b_rewrites = apply_phase_b_final_pass(
        phase_a_internals_source,
        cluster_refactor_responses,
    )
    updated, _pruned = apply_phase_c(phase_b_source)
    validate_refactored_internals(updated)
    return updated


@pytest.fixture(scope="session")
def refactored_package_root(
    tmp_path_factory,
    codegen_package_root,
    phase_bc_internals_source,
) -> Path:
    root = tmp_path_factory.mktemp("tiny_dsa_refactored")
    shutil.copytree(codegen_package_root / "tiny_dsa", root / "tiny_dsa")
    (root / "tiny_dsa" / "internals.py").write_text(
        phase_bc_internals_source,
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="session")
def refactored_tiny_dsa_api(refactored_package_root):
    import importlib

    root = str(refactored_package_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    api = importlib.import_module("tiny_dsa.api")
    return importlib.reload(api)


@pytest.fixture
def shock_cluster_context(tiny_dsa_refactor_projection, codegen_internals_path):
    cluster = next(
        cluster
        for cluster in cluster_graph_formulas(tiny_dsa_refactor_projection)
        if cluster.row == 10 and len(cluster.members) == 5
    )
    ctx = build_cluster_refactor_context(
        tiny_dsa_refactor_projection,
        cluster,
        codegen_internals_path,
    )
    assert ctx is not None
    return ctx
