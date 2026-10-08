"""Configuration validation without network, environment, or verifier access."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class FingerprintIdentity(BaseModel):
    """Identity fields only; calculating these values belongs to a later task."""

    model_config = ConfigDict(extra="forbid")

    declared_model: str
    prompt_sha256: str
    verifier_commit_sha: str
    case_set_sha256: str
    tool_version: str


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: HttpUrl
    # Relative paths are relative to gate.yaml, and are not inspected here.
    verifier_path: Path
    model: str
    expected_fingerprint: FingerprintIdentity | None = None


class Targets(BaseModel):
    """Exactly the two named targets supported by this project."""

    model_config = ConfigDict(extra="forbid")

    current: Target
    candidate: Target


class GateRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_critical_dangerous_mistakes: int = Field(default=0, ge=0)
    min_correct_cases: int | None = Field(default=None, ge=0)
    max_slow_case_seconds: float = Field(default=5.0, gt=0)
    max_dangerous_regressions: int = Field(default=0, ge=0)


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: Targets
    rules: GateRules = Field(default_factory=GateRules)


def load_config(path: str | Path) -> GateConfig:
    """Open only the supplied YAML file, parse it, and validate its data."""
    with Path(path).open(encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    return GateConfig.model_validate(data)
