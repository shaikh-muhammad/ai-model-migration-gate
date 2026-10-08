"""Write-once canonical successes and append-only raw attempts; no scoring."""

from enum import Enum
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


TargetName = Literal["current", "candidate"]
ATTEMPT_FILENAME = re.compile(r"[0-9]{4}\.json")
MAX_ATTEMPT_NUMBER = 9999


class ResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    target: TargetName
    status: ResultStatus
    http_status: int | None = None
    response_time_ms: float = Field(ge=0)
    raw_response: JsonValue | None = None
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def require_success_response(self) -> "CaseResult":
        if self.status == ResultStatus.SUCCESS:
            if self.http_status is None or not 200 <= self.http_status < 300:
                raise ValueError("Successful results require http_status in the HTTP 2xx range")
            if self.raw_response is None:
                raise ValueError("Successful results require raw_response")
        return self


def validate_path_component(name: str, value: str) -> None:
    if not value or value in (".", "..") or any(char in value for char in "/\\\0"):
        raise ValueError(f"{name} must be a nonempty filename component")


def run_path(results_dir: str | Path, target: TargetName, run_id: str) -> Path:
    """Shared directory and component rules for manifests and case results."""
    if target not in ("current", "candidate"):
        raise ValueError("target must be current or candidate")
    validate_path_component("run_id", run_id)
    return Path(results_dir) / target / run_id


def result_path(
    results_dir: str | Path, target: TargetName, run_id: str, case_id: str
) -> Path:
    """Keep each explicit run and case inside its own path component."""
    directory = run_path(results_dir, target, run_id)
    validate_path_component("case_id", case_id)
    if case_id == "manifest":
        raise ValueError("case_id 'manifest' is reserved for the run manifest")
    return directory / f"{case_id}.json"


def read_result(
    results_dir: str | Path, target: TargetName, run_id: str, case_id: str
) -> CaseResult | None:
    path = result_path(results_dir, target, run_id, case_id)
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Canonical result must be a regular file: {path}")
    result = CaseResult.model_validate_json(path.read_text(encoding="utf-8"))
    if result.case_id != case_id or result.target != target:
        raise ValueError(f"Result identity does not match its path: {path}")
    if result.status != ResultStatus.SUCCESS:
        raise ValueError(f"Canonical result must have status success: {path}")
    return result


def _write_once(path: Path, result: CaseResult) -> Path:
    # Revalidate to prevent mutated/model_copy instances from persisting invalid data.
    payload = CaseResult.model_validate(result.model_dump()).model_dump_json(indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(payload)
    return path


def write_result(results_dir: str | Path, run_id: str, result: CaseResult) -> Path:
    """Exclusively create an authoritative success; existing files always fail."""
    if result.status != ResultStatus.SUCCESS:
        raise ValueError("Canonical result must have status success")
    return _write_once(result_path(results_dir, result.target, run_id, result.case_id), result)


def attempt_directory(
    results_dir: str | Path, target: TargetName, run_id: str, case_id: str,
) -> Path:
    # Reuse canonical path validation, including its reserved manifest case ID.
    canonical = result_path(results_dir, target, run_id, case_id)
    return canonical.parent / "attempts" / case_id


def attempt_path(
    results_dir: str | Path, target: TargetName, run_id: str, case_id: str,
    attempt_number: int,
) -> Path:
    directory = attempt_directory(results_dir, target, run_id, case_id)
    if (not isinstance(attempt_number, int) or isinstance(attempt_number, bool)
            or not 1 <= attempt_number <= MAX_ATTEMPT_NUMBER):
        raise ValueError(f"attempt_number must be an integer from 1 to {MAX_ATTEMPT_NUMBER}")
    return directory / f"{attempt_number:04d}.json"


def next_attempt_number(
    results_dir: str | Path, target: TargetName, run_id: str, case_id: str,
) -> int:
    """Validate names and allocate above the maximum; never fill existing gaps."""
    directory = attempt_directory(results_dir, target, run_id, case_id)
    if not directory.exists() and not directory.is_symlink():
        return 1
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Attempt history must be a regular directory: {directory}")
    highest = 0
    for entry in sorted(directory.iterdir()):
        if (not ATTEMPT_FILENAME.fullmatch(entry.name)
                or not entry.is_file() or entry.is_symlink()):
            raise ValueError(f"Malformed attempt history entry: {entry}")
        number = int(entry.stem)
        if number == 0:
            raise ValueError(f"Malformed attempt history entry: {entry}")
        highest = max(highest, number)
    if highest == MAX_ATTEMPT_NUMBER:
        raise ValueError(f"Attempt numbering exhausted for case {case_id!r}")
    return highest + 1


def write_attempt(
    results_dir: str | Path, run_id: str, attempt_number: int, result: CaseResult,
) -> Path:
    """Append exactly the allocated record, failing safely on duplicate creation."""
    path = attempt_path(results_dir, result.target, run_id, result.case_id, attempt_number)
    return _write_once(path, result)
