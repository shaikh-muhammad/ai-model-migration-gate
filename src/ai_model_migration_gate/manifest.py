"""Immutable run metadata and fingerprint validation before confirmed execution."""

from datetime import datetime, timezone
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictStr, field_validator

from .cases import Case
from .config import FingerprintIdentity, GateConfig
from .results import TargetName, run_path


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: StrictStr
    target: TargetName
    created_at: AwareDatetime
    fingerprint: FingerprintIdentity

    @field_validator("created_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


def manifest_path(results_dir: str | Path, target: TargetName, run_id: str) -> Path:
    return run_path(results_dir, target, run_id) / "manifest.json"


def create_manifest(results_dir: str | Path, manifest: RunManifest) -> Path:
    """Exclusive creation: an existing manifest can never be replaced here."""
    path = manifest_path(results_dir, manifest.target, manifest.run_id)
    payload = manifest.model_dump_json(indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(payload)
    return path


def require_fingerprint_match(
    actual: FingerprintIdentity, expected: FingerprintIdentity,
) -> None:
    differences = [
        field for field in FingerprintIdentity.model_fields
        if getattr(actual, field) != getattr(expected, field)
    ]
    if differences:
        raise ValueError("Fingerprint mismatch: " + ", ".join(differences))


def prepare_run_manifest(
    *, config: GateConfig, target: TargetName, run_id: str,
    config_path: str | Path, cases: list[Case], results_dir: str | Path,
    created_at: datetime | None = None,
) -> RunManifest:
    """Validate the full corpus identity, creating only new manifest-backed runs."""
    # Fingerprinting shares the runner's image resolver; import at call time to
    # avoid a module initialization cycle when the runner imports this module.
    from .fingerprint import calculate_fingerprint

    path = manifest_path(results_dir, target, run_id)
    expected = getattr(config.targets, target).expected_fingerprint
    if expected is None:
        raise ValueError(
            f"Target {target!r} requires a non-null expected_fingerprint for confirmed execution"
        )

    if path.exists() or path.is_symlink():
        manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
        if manifest.run_id != run_id:
            raise ValueError("Manifest run_id does not match its path/requested run ID")
        if manifest.target != target:
            raise ValueError("Manifest target does not match its path/requested target")
        require_fingerprint_match(manifest.fingerprint, expected)
        current = calculate_fingerprint(config, target, config_path, cases)
        require_fingerprint_match(current, manifest.fingerprint)
        return manifest

    if path.parent.exists() and any(path.parent.iterdir()):
        raise ValueError("Pre-manifest or invalid run directory: a new run ID is required")
    current = calculate_fingerprint(config, target, config_path, cases)
    require_fingerprint_match(current, expected)
    manifest = RunManifest(
        run_id=run_id, target=target,
        created_at=created_at if created_at is not None else datetime.now(timezone.utc),
        fingerprint=current,
    )
    create_manifest(results_dir, manifest)
    return manifest
