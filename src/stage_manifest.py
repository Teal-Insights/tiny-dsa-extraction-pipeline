"""Per-stage pipeline manifests for mid-pipeline entry and fingerprint checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from src.codegen_cache import guide_fingerprint
from src.graph_cache import bindings_fingerprint, file_fingerprint, stable_json
from src.pipeline_config import PipelineConfig

STAGE_MANIFEST_SCHEMA_VERSION = "1.0.0"
STAGES_DIRNAME = "stages"

PipelineStageName = str  # narrowed by callers to PIPELINE_STAGES members

_UPSTREAM_STAGE: dict[str, str] = {
    "export": "extract",
    "validate": "export",
    "annotate": "validate",
    "document": "annotate",
}


class StageManifestError(RuntimeError):
    """Base error for stage-manifest failures."""


class StageManifestMissingError(StageManifestError):
    """Raised when a required stage manifest file is absent."""


class StageManifestDriftError(StageManifestError):
    """Raised when recomputed input fingerprints disagree with a manifest."""


@dataclass(frozen=True)
class StageManifest:
    """On-disk record of a completed pipeline stage."""

    stage: str
    schema_version: str
    cache_keys: dict[str, str]
    upstream_keys: dict[str, str]
    fingerprints: dict[str, str]

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "schema_version": self.schema_version,
            "cache_keys": dict(self.cache_keys),
            "upstream_keys": dict(self.upstream_keys),
            "fingerprints": dict(self.fingerprints),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object], *, path: Path
    ) -> StageManifest:
        if not isinstance(payload, dict):
            raise TypeError(f"invalid stage manifest payload: {path}")
        stage = payload.get("stage")
        schema_version = payload.get("schema_version")
        cache_keys = payload.get("cache_keys")
        upstream_keys = payload.get("upstream_keys")
        fingerprints = payload.get("fingerprints")
        if not isinstance(stage, str) or not stage:
            raise ValueError(f"stage manifest missing stage: {path}")
        if not isinstance(schema_version, str) or not schema_version:
            raise ValueError(f"stage manifest missing schema_version: {path}")
        if schema_version != STAGE_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported stage manifest schema_version {schema_version!r} "
                f"(expected {STAGE_MANIFEST_SCHEMA_VERSION!r}): {path}"
            )
        if not isinstance(cache_keys, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in cache_keys.items()
        ):
            raise ValueError(f"stage manifest has invalid cache_keys: {path}")
        if not isinstance(upstream_keys, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in upstream_keys.items()
        ):
            raise ValueError(f"stage manifest has invalid upstream_keys: {path}")
        if not isinstance(fingerprints, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in fingerprints.items()
        ):
            raise ValueError(f"stage manifest has invalid fingerprints: {path}")
        typed_cache_keys = {str(key): str(value) for key, value in cache_keys.items()}
        typed_upstream_keys = {
            str(key): str(value) for key, value in upstream_keys.items()
        }
        typed_fingerprints = {
            str(key): str(value) for key, value in fingerprints.items()
        }
        return cls(
            stage=stage,
            schema_version=schema_version,
            cache_keys=typed_cache_keys,
            upstream_keys=typed_upstream_keys,
            fingerprints=typed_fingerprints,
        )


def stage_manifest_path(repo_root: Path, stage: str) -> Path:
    return repo_root / "artifacts" / STAGES_DIRNAME / f"{stage}.json"


def compute_input_fingerprints(config: PipelineConfig) -> dict[str, str]:
    """Return labeled fingerprints for pipeline inputs that gate stage entry."""
    return {
        "workbook": file_fingerprint(config.workbook_path),
        "bindings": bindings_fingerprint(config.bindings_path),
        "constraints": hashlib.sha256(
            stable_json(dict(config.constraints)).encode()
        ).hexdigest(),
        "targets": hashlib.sha256(
            stable_json(sorted(config.targets)).encode()
        ).hexdigest(),
        "guide": guide_fingerprint(config.guide_path),
        "excel_grapher_version": version("excel-grapher"),
        "internal_binding_validation_mode": str(
            config.internal_binding_validation_mode
        ),
        "internal_binding_exempt_cells": hashlib.sha256(
            stable_json(sorted(config.internal_binding_exempt_cells)).encode()
        ).hexdigest(),
    }


def write_stage_manifest(
    config: PipelineConfig,
    *,
    stage: str,
    cache_keys: Mapping[str, str],
    upstream_keys: Mapping[str, str],
    fingerprints: Mapping[str, str] | None = None,
) -> StageManifest:
    """Persist a stage manifest under ``artifacts/stages/`` and return it."""
    resolved_fingerprints = (
        dict(fingerprints)
        if fingerprints is not None
        else compute_input_fingerprints(config)
    )
    manifest = StageManifest(
        stage=stage,
        schema_version=STAGE_MANIFEST_SCHEMA_VERSION,
        cache_keys=dict(cache_keys),
        upstream_keys=dict(upstream_keys),
        fingerprints=resolved_fingerprints,
    )
    path = stage_manifest_path(config.repo_root, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_stage_manifest(repo_root: Path, stage: str) -> StageManifest:
    """Load a stage manifest or raise ``StageManifestMissingError``."""
    path = stage_manifest_path(repo_root, stage)
    if not path.is_file():
        raise StageManifestMissingError(
            f"missing stage manifest for {stage!r}: {path.as_posix()}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return StageManifest.from_payload(payload, path=path)


def assert_manifest_fresh(manifest: StageManifest, config: PipelineConfig) -> None:
    """Recompute fingerprints and fail loudly when any labeled input drifted.

    The label sets are compared both ways on purpose. Iterating only the stored
    labels would silently skip any input added to
    :func:`compute_input_fingerprints` after the manifest was written, which
    makes every new label depend on someone also bumping
    ``STAGE_MANIFEST_SCHEMA_VERSION``. That version guards the manifest's file
    structure; which inputs are covered is a separate axis, so a manifest whose
    label set does not match the current one is stale by definition.
    """
    current = compute_input_fingerprints(config)
    missing = sorted(set(manifest.fingerprints) - set(current))
    if missing:
        raise StageManifestDriftError(
            f"{', '.join(missing)} fingerprint missing from current inputs "
            f"(stage={manifest.stage!r})"
        )
    unchecked = sorted(set(current) - set(manifest.fingerprints))
    if unchecked:
        raise StageManifestDriftError(
            f"manifest predates the {', '.join(unchecked)} fingerprint and cannot "
            f"be verified (stage={manifest.stage!r}); re-run the upstream stage"
        )
    for name, expected in manifest.fingerprints.items():
        if current[name] != expected:
            raise StageManifestDriftError(
                f"{name} fingerprint drifted (stage={manifest.stage!r})"
            )


def upstream_stage_name(start_from_stage: str) -> str | None:
    """Return the stage whose manifest must exist to enter ``start_from_stage``."""
    return _UPSTREAM_STAGE.get(start_from_stage)


def require_upstream_manifest(
    config: PipelineConfig,
    *,
    start_from_stage: str,
) -> StageManifest:
    """Load and freshness-check the manifest required to enter ``start_from_stage``."""
    upstream = upstream_stage_name(start_from_stage)
    if upstream is None:
        raise ValueError(
            f"stage {start_from_stage!r} has no upstream manifest requirement"
        )
    path = stage_manifest_path(config.repo_root, upstream)
    try:
        manifest = load_stage_manifest(config.repo_root, upstream)
    except StageManifestMissingError as error:
        raise StageManifestMissingError(
            f"cannot start from {start_from_stage!r}: missing upstream manifest "
            f"{path.name} at {path.as_posix()}"
        ) from error
    assert_manifest_fresh(manifest, config)
    return manifest
