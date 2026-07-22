"""Run standalone Pass-2 semantic naming on a mechanical ``internals.py``.

Discovers helpers still marked pending semantic naming, asks the configured
refactor model for docstring + local renames, applies them, and writes the
module. No dependency graph, clustering, or parity gate is required.

v1 prompts are thinner than in-pipeline Pass 2 (Note + body + renameable
locals only; no full fingerprint context), so naming quality may differ
slightly from a live Pass 2 run.

Usage:
    uv run python -m scripts.run_semantic_naming --internals dist/<package>/internals.py
    uv run python -m scripts.run_semantic_naming --internals path/to/internals.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from src.async_gather import run_map_as_completed
from src.internals_refactor import (
    _MECHANICAL_NAMING_SYSTEM_PROMPT,
    raise_if_llm_declared_error,
    refactor_model,
)
from src.llm_json import DEFAULT_MAX_ATTEMPTS, generate_validated_json_async
from src.llm_providers import build_async_client, get_llm_semaphore
from src.logging_config import configure_logging
from src.mechanical_naming import (
    ClusterNamingLLMResponse,
    SingletonNamingLLMResponse,
    apply_cluster_naming_response,
)
from src.standalone_semantic_naming import (
    DiscoveredNamingHelper,
    discover_pending_naming_helpers,
    forbidden_names_for_standalone,
    run_standalone_semantic_naming,
)

logger = logging.getLogger(__name__)


def _gather_llm_responses(
    misses: Sequence[DiscoveredNamingHelper],
    prompts: Mapping[str, str],
    *,
    forbidden_names: frozenset[str],
    on_success: Callable[[str, ClusterNamingLLMResponse], None] | None = None,
) -> Mapping[str, ClusterNamingLLMResponse]:
    model = refactor_model()
    client, provider = build_async_client(model)
    semaphore = get_llm_semaphore()

    def _make_post_validate(helper: DiscoveredNamingHelper):
        def _post_validate(
            parsed: ClusterNamingLLMResponse,
        ) -> ClusterNamingLLMResponse:
            raise_if_llm_declared_error(
                parsed,
                kind=helper.kind,
                target=helper.helper_name,
            )
            apply_cluster_naming_response(
                parsed,
                helper.draft,
                parameter_names=helper.parameter_names,
                forbidden_names=forbidden_names,
            )
            return parsed

        return _post_validate

    async def _one(
        helper: DiscoveredNamingHelper,
    ) -> tuple[str, ClusterNamingLLMResponse]:
        response_model: type[ClusterNamingLLMResponse] = (
            ClusterNamingLLMResponse
            if helper.kind == "cluster"
            else SingletonNamingLLMResponse
        )
        parsed, _content = await generate_validated_json_async(
            client=client,
            model=model,
            provider=provider,
            system_prompt=_MECHANICAL_NAMING_SYSTEM_PROMPT,
            user_prompt=prompts[helper.helper_name],
            response_model=response_model,
            post_validate=_make_post_validate(helper),
            max_attempts=DEFAULT_MAX_ATTEMPTS,
            semaphore=semaphore,
        )
        return helper.helper_name, parsed

    return run_map_as_completed(misses, _one, on_success=on_success)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run Pass-2 semantic naming from a mechanical internals.py alone "
            "(no graph / clustering / parity gate)."
        )
    )
    parser.add_argument(
        "--internals",
        type=Path,
        required=True,
        help="Path to mechanical internals.py pending semantic naming",
    )
    parser.add_argument(
        "--runtime",
        type=Path,
        default=None,
        help="Optional runtime.py for forbidden-name allowlist (default: sibling)",
    )
    parser.add_argument(
        "--readers",
        type=Path,
        default=None,
        help="Optional _readers.py for forbidden-name allowlist (default: sibling)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Apply naming in memory only; do not write internals.py "
            "(cache updates are also skipped)"
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore and do not update .cache/internals-refactors.json",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Discover and print pending helpers, then exit",
    )
    args = parser.parse_args(argv)

    configure_logging()
    internals_path = args.internals
    source = internals_path.read_text(encoding="utf-8")
    helpers = discover_pending_naming_helpers(source)
    if args.list_only:
        for helper in helpers:
            print(
                f"{helper.kind}\t{helper.helper_name}\t"
                f"locals={list(helper.draft.renameable_locals)}\t"
                f"tables={list(helper.draft.lookup_table_names)}"
            )
        print(f"{len(helpers)} pending helper(s)")
        return 0

    runtime_path = args.runtime
    readers_path = args.readers
    if runtime_path is None:
        sibling = internals_path.with_name("runtime.py")
        if sibling.is_file():
            runtime_path = sibling
    if readers_path is None:
        sibling = internals_path.with_name("_readers.py")
        if sibling.is_file():
            readers_path = sibling
    if runtime_path is None:
        logger.warning(
            "no runtime.py beside %s; forbidden-name checks omit runtime symbols",
            internals_path,
        )

    logger.info(
        "standalone semantic naming: %s pending helper(s) in %s "
        "(v1 thin prompts; no fingerprint context)",
        len(helpers),
        internals_path,
    )
    forbidden = forbidden_names_for_standalone(
        source,
        runtime_path=runtime_path,
        readers_path=readers_path,
    )

    def _gather(
        misses: Sequence[DiscoveredNamingHelper],
        prompts: Mapping[str, str],
        *,
        on_success: Callable[[str, ClusterNamingLLMResponse], None] | None = None,
    ) -> Mapping[str, ClusterNamingLLMResponse]:
        return _gather_llm_responses(
            misses,
            prompts,
            forbidden_names=forbidden,
            on_success=on_success,
        )

    named = run_standalone_semantic_naming(
        source,
        gather_responses=_gather,
        runtime_path=runtime_path,
        readers_path=readers_path,
        internals_path=internals_path,
        dry_run=args.dry_run,
        use_cache=not args.no_cache,
    )
    if args.dry_run:
        logger.info(
            "dry-run complete (%s bytes); internals not written",
            len(named),
        )
    else:
        logger.info("wrote named internals to %s", internals_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
