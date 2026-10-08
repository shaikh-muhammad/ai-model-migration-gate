"""Run planning and confirmed execution through an injected HTTP client factory."""

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import time
from urllib.parse import urljoin

import httpx

from .cases import Case
from .config import GateConfig, Target
from .manifest import prepare_run_manifest
from .results import (
    CaseResult, ResultStatus, TargetName, next_attempt_number, read_result,
    result_path, write_attempt, write_result,
)


MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_APPLICATION_CHARACTERS = 16_000
REQUEST_TIMEOUT_SECONDS = 30.0
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass
class RunPlan:
    target: TargetName
    run_id: str | None
    confirmed: bool
    selected_cases: list[Case]
    skipped_case_ids: list[str]


@dataclass
class RecordedAttempt:
    result: CaseResult
    attempt_number: int
    attempt_path: Path
    canonical_path: Path | None


@dataclass
class ExecutionSummary:
    attempts: list[RecordedAttempt]
    skipped_success_count: int

    @property
    def attempted_count(self) -> int:
        return len(self.attempts)

    @property
    def successful_attempt_count(self) -> int:
        return sum(attempt.result.status == ResultStatus.SUCCESS for attempt in self.attempts)

    @property
    def error_attempt_count(self) -> int:
        return sum(attempt.result.status == ResultStatus.ERROR for attempt in self.attempts)


def plan_run(
    cases: list[Case], *, target: TargetName, max_calls: int,
    results_dir: str | Path, run_id: str | None = None,
    case_id: str | None = None, force: bool = False, yes: bool = False,
) -> RunPlan:
    """Select at most the call budget; inspect only this target and explicit run."""
    if target not in ("current", "candidate"):
        raise ValueError("target must be current or candidate")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls <= 0:
        raise ValueError("max_calls must be a positive integer")
    if run_id is not None:
        result_path(results_dir, target, run_id, "validation")
    if case_id is not None:
        cases = [case for case in cases if case.id == case_id]
        if not cases:
            raise ValueError(f"Unknown case ID: {case_id}")
    selected = []
    skipped = []
    for case in cases:
        existing = (
            read_result(results_dir, target, run_id, case.id)
            if run_id is not None else None
        )
        if existing is not None and existing.status == ResultStatus.SUCCESS and not force:
            skipped.append(case.id)
            continue
        selected.append(case)
        if len(selected) == max_calls:
            break
    return RunPlan(target, run_id, yes, selected, skipped)


@dataclass
class PreparedCase:
    image_path: Path
    mime_type: str
    application_json: str


def resolve_image_path(case: Case, config_path: str | Path) -> Path:
    return (Path(config_path).resolve().parent / case.image).resolve()


def preflight_case(case: Case, config_path: str | Path) -> PreparedCase:
    """Validate locally without changing the image or opening a connection."""
    image_path = resolve_image_path(case, config_path)
    if not image_path.exists():
        raise ValueError(f"Image does not exist: {image_path}")
    if not image_path.is_file():
        raise ValueError(f"Image must be a regular file: {image_path}")
    mime_type = IMAGE_MIME_TYPES.get(image_path.suffix.lower())
    if mime_type is None:
        raise ValueError("Image must use .jpg, .jpeg, .png, or .webp")
    if image_path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES} bytes: {image_path}")
    with image_path.open("rb") as image_file:
        header = image_file.read(12)
    matches_type = {
        "image/jpeg": header.startswith(b"\xff\xd8\xff"),
        "image/png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": header[:4] == b"RIFF" and header[8:12] == b"WEBP",
    }
    if not matches_type[mime_type]:
        raise ValueError(f"Image content does not match {image_path.suffix}: {image_path}")
    application_json = json.dumps(
        case.application.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(application_json) > MAX_APPLICATION_CHARACTERS:
        raise ValueError(
            f"Application JSON exceeds {MAX_APPLICATION_CHARACTERS} characters"
        )
    return PreparedCase(image_path, mime_type, application_json)


def _error_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("error_code", "errorCode", "code"):
        if isinstance(payload.get(key), str):
            return payload[key]
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("error_code", "errorCode", "code"):
            if isinstance(error.get(key), str):
                return error[key]
    return None


def call_case(
    case: Case, *, target: TargetName, target_config: Target,
    config_path: str | Path, client: httpx.Client, confirmed: bool = False,
) -> CaseResult:
    """One confirmed attempt; callers must supply the client and handle storage."""
    if not confirmed:
        raise ValueError("Explicit confirmation (--yes) is required before a case call")
    if target not in ("current", "candidate"):
        raise ValueError("target must be current or candidate")
    prepared = preflight_case(case, config_path)
    url = urljoin(str(target_config.base_url), "/api/verify")
    with prepared.image_path.open("rb") as image_file:
        started = time.perf_counter()
        try:
            response = client.post(
                url,
                data={"application": prepared.application_json},
                files={"image": (prepared.image_path.name, image_file, prepared.mime_type)},
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            return CaseResult(
                case_id=case.id, target=target, status=ResultStatus.ERROR,
                response_time_ms=(time.perf_counter() - started) * 1000,
                error_message=str(exc),
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        payload = response.json()
    except ValueError:
        return CaseResult(
            case_id=case.id, target=target, status=ResultStatus.ERROR,
            http_status=response.status_code, response_time_ms=elapsed_ms,
            raw_response=response.text, error_message="Response was not valid JSON.",
        )
    return CaseResult(
        case_id=case.id, target=target,
        status=ResultStatus.SUCCESS if response.is_success else ResultStatus.ERROR,
        http_status=response.status_code, response_time_ms=elapsed_ms,
        raw_response=payload,
        error_code=_error_code(payload) if not response.is_success else None,
    )


def execute_plan(
    plan: RunPlan, *, config: GateConfig, cases: list[Case], config_path: str | Path,
    results_dir: str | Path, client_factory: Callable[..., httpx.Client],
    on_recorded: Callable[[RecordedAttempt], None] | None = None,
) -> ExecutionSummary:
    """Preflight the whole batch, reuse one client, and save every attempt immediately."""
    if not plan.confirmed:
        raise ValueError("Explicit confirmation (--yes) is required for execution")
    if plan.run_id is None:
        raise ValueError("--run-id is required with --yes")
    result_path(results_dir, plan.target, plan.run_id, "validation")
    for case in plan.selected_cases:
        preflight_case(case, config_path)
    for case in plan.selected_cases:
        result_path(results_dir, plan.target, plan.run_id, case.id)
        read_result(results_dir, plan.target, plan.run_id, case.id)
        next_attempt_number(results_dir, plan.target, plan.run_id, case.id)
    for case_id in plan.skipped_case_ids:
        read_result(results_dir, plan.target, plan.run_id, case_id)
    prepare_run_manifest(
        config=config, target=plan.target, run_id=plan.run_id,
        config_path=config_path, cases=cases, results_dir=results_dir,
    )
    summary = ExecutionSummary([], len(plan.skipped_case_ids))
    if not plan.selected_cases:
        return summary
    target_config = getattr(config.targets, plan.target)
    with client_factory(
        trust_env=False, follow_redirects=False, timeout=REQUEST_TIMEOUT_SECONDS,
    ) as client:
        for case in plan.selected_cases:
            number = next_attempt_number(results_dir, plan.target, plan.run_id, case.id)
            result = call_case(
                case, target=plan.target, target_config=target_config,
                config_path=config_path, client=client, confirmed=True,
            )
            path = write_attempt(results_dir, plan.run_id, number, result)
            canonical_path = None
            if result.status == ResultStatus.SUCCESS:
                if read_result(results_dir, plan.target, plan.run_id, case.id) is None:
                    canonical_path = write_result(results_dir, plan.run_id, result)
            attempt = RecordedAttempt(result, number, path, canonical_path)
            summary.attempts.append(attempt)
            if on_recorded is not None:
                on_recorded(attempt)
    return summary
