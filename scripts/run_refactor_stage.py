"""Run only the internals refactor stage against an isolated codegen root.

Thin wrapper over ``python -m src.extraction_pipeline --only-stage refactor``
with lab-specific flags. Rebuilds from the warm export stage manifest (or runs
export first when entering via a full pipeline start). By default writes into
``artifacts/refactor-lab/`` so ``dist/`` stays untouched unless ``--in-place``.

Usage:
    uv run python -m scripts.run_refactor_stage [--dump-prompts DIR]
        [--no-parity-gate] [--dry-run] [--in-place] [--output-root DIR]
        [--no-cache] [--clustering-mode MODE] [--variation-mode MODE]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from src.extraction_pipeline import RefactorLabOptions, run_pipeline
from src.internals_refactor import (
    ClusterRefactorContext,
    SingletonRefactorContext,
    set_cluster_context_observer,
    set_refactor_prompt_observer,
    set_singleton_context_observer,
)
from src.mechanical_body import (
    MechanicalSynthesisError,
    synthesize_cluster_body,
    synthesize_singleton_body,
)
from src.logging_config import configure_logging
from src.pipeline_config import (
    add_clustering_mode_argument,
    add_variation_mode_argument,
    apply_clustering_mode_cli_override,
    apply_variation_mode_cli_override,
    load_pipeline_config,
    validate_pipeline_config,
)

DEFAULT_OUTPUT_ROOT = Path("artifacts/refactor-lab")


def _synthesis_reporter(report_dir: Path):
    """Return observers that try mechanical synthesis on each refactor context."""
    report_dir.mkdir(parents=True, exist_ok=True)
    results: list[str] = []

    def _record_draft(name: str, draft) -> None:
        results.append(
            f"{name}: OK (groups={draft.group_count}, "
            f"locals={list(draft.renameable_locals)}, "
            f"tables={list(draft.lookup_table_names)})"
        )
        (report_dir / f"{name}.draft.py").write_text(
            draft.body + "\n", encoding="utf-8", newline="\n"
        )

    def _observe_cluster(ctx: ClusterRefactorContext) -> None:
        name = ctx.expected_helper_name
        if ctx.fingerprint_summary is None:
            results.append(f"{name}: SKIP (no fingerprint summary)")
            return
        try:
            draft = synthesize_cluster_body(
                ctx.fingerprint_summary,
                key_vocabulary=ctx.key_vocabulary,
                expected_member_keys=ctx.expected_member_keys,
                helper_name=name,
            )
        except MechanicalSynthesisError as error:
            results.append(f"{name}: FAIL ({error.reason})")
            return
        _record_draft(name, draft)

    def _observe_singleton(ctx: SingletonRefactorContext) -> None:
        name = ctx.expected_helper_name
        try:
            draft = synthesize_singleton_body(
                ctx.python_source,
                inline_replacements=dict(ctx.inline_replacements),
            )
        except MechanicalSynthesisError as error:
            results.append(f"{name}: FAIL ({error.reason})")
            return
        _record_draft(name, draft)

    return _observe_cluster, _observe_singleton, results


def _prompt_dump_observer(dump_dir: Path):
    """Return an observer writing one ``{kind}_{target}.md`` file per prompt.

    Files are keyed by unit identity (kind + target) with no ordering counter, so
    concurrent pass-2 dumps stay collision-free and a repeat prompt for the same
    unit overwrites its earlier dump rather than accumulating duplicates.
    """
    dump_dir.mkdir(parents=True, exist_ok=True)

    def _observe(kind: str, target: str, prompt: str) -> None:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", target).strip("_") or "unit"
        path = dump_dir / f"{kind}_{slug}.md"
        path.write_text(prompt, encoding="utf-8", newline="\n")

    return _observe


def main(argv: Sequence[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Run only the internals refactor stage in an isolated root."
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            "Directory receiving the regenerated package modules and refactor "
            f"output (default: {DEFAULT_OUTPUT_ROOT})."
        ),
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Target dist/ (the real pipeline output) instead of --output-root.",
    )
    parser.add_argument(
        "--dump-prompts",
        type=Path,
        default=None,
        metavar="DIR",
        help="Write every refactor prompt to DIR as it is built.",
    )
    parser.add_argument(
        "--no-parity-gate",
        action="store_true",
        help="Skip the per-helper differential parity gate (faster, less safe).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and validate refactors without writing internals.py.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Bypass graph/projection/series-resolution/series-derived/codegen/"
            "cluster caches for this run."
        ),
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help=(
            "Rebuild warm caches even when keys match. The lab flags above "
            "already force a real refactor run; use this to also discard warm "
            "graph/projection/cluster payloads."
        ),
    )
    parser.add_argument(
        "--report-synthesis",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Attempt mechanical body synthesis for every refactor unit (clusters "
            "and singletons), write verified drafts to DIR, and print an OK/FAIL "
            "summary."
        ),
    )
    add_variation_mode_argument(parser)
    add_clustering_mode_argument(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = apply_clustering_mode_cli_override(
        apply_variation_mode_cli_override(load_pipeline_config(), args.variation_mode),
        args.clustering_mode,
    )
    validate_pipeline_config(config)
    if not args.in_place:
        config = replace(config, dist_root=args.output_root.resolve())

    prompt_observer = None
    if args.dump_prompts is not None:
        prompt_observer = _prompt_dump_observer(args.dump_prompts)
    synthesis_results: list[str] | None = None
    cluster_observer = None
    singleton_observer = None
    if args.report_synthesis is not None:
        cluster_observer, singleton_observer, synthesis_results = _synthesis_reporter(
            args.report_synthesis
        )

    lab_options = RefactorLabOptions(
        dry_run=args.dry_run,
        parity_gate=not args.no_parity_gate,
        prompt_observer=prompt_observer,
        cluster_context_observer=cluster_observer,
        singleton_context_observer=singleton_observer,
    )
    try:
        run_pipeline(
            config,
            only_stage="refactor",
            no_cache=args.no_cache,
            force_rebuild=args.force_rebuild,
            lab_options=lab_options,
        )
    finally:
        set_refactor_prompt_observer(None)
        set_cluster_context_observer(None)
        set_singleton_context_observer(None)

    internals_path = config.package_root / "internals.py"
    print(f"Refactored internals at {internals_path}")
    if args.dump_prompts is not None:
        prompt_count = len(list(args.dump_prompts.glob("*.md")))
        print(f"Dumped {prompt_count} prompt(s) to {args.dump_prompts}")
    if synthesis_results is not None:
        print("Mechanical synthesis report:")
        for line in synthesis_results:
            print(f"  {line}")


if __name__ == "__main__":
    main()
