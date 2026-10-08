"""Read-only fingerprint calculation from declared configuration and exact bytes."""

import hashlib
import json
from pathlib import Path
import subprocess

import ai_model_migration_gate

from .cases import Case
from .config import FingerprintIdentity, GateConfig
from .runner import resolve_image_path


PROMPT_RELATIVE_PATH = Path("lib/label-extraction-prompt.ts")


def file_sha256(path: Path, description: str) -> str:
    """Hash the complete file without decoding or normalizing its contents."""
    if not path.exists():
        raise ValueError(f"{description} does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"{description} must be a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompt_sha256(verifier_path: str | Path) -> str:
    return file_sha256(Path(verifier_path) / PROMPT_RELATIVE_PATH, "Prompt file")


def _git(verifier_path: Path, *arguments: str) -> str:
    """Inspect Git without index refresh writes or filesystem-monitor hooks."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-C", str(verifier_path), *arguments],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("Git is required to calculate verifier identity") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"Cannot inspect verifier Git repository at {verifier_path}: {exc.stderr.strip()}"
        ) from exc
    return result.stdout.strip()


def verifier_commit_sha(verifier_path: str | Path) -> str:
    """Require a clean tracked/untracked tree; Git-ignored files are excluded."""
    verifier_path = Path(verifier_path)
    status = _git(verifier_path, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ValueError(f"Verifier working tree is dirty: {verifier_path}\n{status}")
    return _git(verifier_path, "rev-parse", "--verify", "HEAD")


def canonical_case_json(cases: list[Case], config_path: str | Path) -> str:
    """Return structurally canonical JSON, preserving application content values."""
    records = []
    for case in sorted(cases, key=lambda case: case.id):
        records.append({
            "id": case.id,
            "expected_outcome": case.expected_outcome.value,
            "application": case.application.model_dump(mode="json", exclude_none=True),
            "critical": case.critical,
            "tags": sorted(case.tags),
            "image_sha256": file_sha256(resolve_image_path(case, config_path), "Case image"),
        })
    return json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def case_set_sha256(cases: list[Case], config_path: str | Path) -> str:
    canonical_bytes = canonical_case_json(cases, config_path).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def calculate_fingerprint(
    config: GateConfig, target: str, config_path: str | Path, cases: list[Case],
) -> FingerprintIdentity:
    """Use only the configured model, verifier files/Git, cases, and tool version."""
    if target not in ("current", "candidate"):
        raise ValueError("target must be current or candidate")
    target_config = getattr(config.targets, target)
    if not target_config.model.strip() or target_config.model.strip() == "CHANGE_ME":
        raise ValueError(f"Target {target!r} must declare a nonempty model other than CHANGE_ME")
    verifier_path = (Path(config_path).resolve().parent / target_config.verifier_path).resolve()
    commit_sha = verifier_commit_sha(verifier_path)
    return FingerprintIdentity(
        declared_model=target_config.model,
        prompt_sha256=prompt_sha256(verifier_path),
        verifier_commit_sha=commit_sha,
        case_set_sha256=case_set_sha256(cases, config_path),
        tool_version=ai_model_migration_gate.__version__,
    )
