"""Contract tests for the manual dist/ deploy workflow."""

from pathlib import Path

WORKFLOW_PATH = Path(".github/workflows/deploy.yml")


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_deploy_workflow_resolves_target_from_workbook_config() -> None:
    text = workflow_text()
    assert "repository_slug" in text
    assert "load_pipeline_config" in text


def test_deploy_workflow_pushes_dist_to_target_repo() -> None:
    text = workflow_text()
    assert "DEPLOY_TOKEN" in text
    assert 'rsync -a --delete --exclude ".git/" dist/' in text
    assert "git push origin HEAD:main" in text


def test_deploy_workflow_steps_are_conditional_on_target() -> None:
    text = workflow_text()
    assert text.count("if: steps.target.outputs.slug != ''") >= 3


def test_deploy_workflow_has_no_hardcoded_package_repo() -> None:
    text = workflow_text()
    assert "py-q-craft" not in text
    assert "Teal-Insights" not in text


def test_deploy_workflow_does_not_run_extraction_in_ci() -> None:
    """Publishing must use the committed dist/, never regenerate it in CI."""
    text = workflow_text()
    assert "src.extraction_pipeline" not in text
    assert "extraction_pipeline" not in text
    assert "OPENAI_API_KEY" not in text


def test_deploy_workflow_publishes_committed_dist() -> None:
    text = workflow_text()
    # A guard must fail loudly when the committed dist/ is absent or empty.
    assert "dist/" in text
    assert "No committed dist/" in text or "dist/ is empty" in text
