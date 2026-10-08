"""Offline scoring of complete canonical results, using only a committed pin."""

from collections import Counter
from enum import Enum
import math
from pathlib import Path
from statistics import median

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError, model_validator

from .cases import Case, ExpectedOutcome
from .config import GateConfig
from .manifest import RunManifest, manifest_path, require_fingerprint_match
from .results import CaseResult, ResultStatus, TargetName, read_result, result_path


class Classification(str, Enum):
    CORRECT = "correct"
    MINOR = "minor"
    FALSE_BLOCK = "false_block"
    DANGEROUS = "dangerous"


OUTCOME_MAPPING = {
    "pass": ExpectedOutcome.PASS,
    "needs_review": ExpectedOutcome.NEEDS_REVIEW,
    "fail": ExpectedOutcome.FAIL,
}
CLASSIFICATION_MATRIX = {
    (ExpectedOutcome.PASS, ExpectedOutcome.PASS): Classification.CORRECT,
    (ExpectedOutcome.PASS, ExpectedOutcome.NEEDS_REVIEW): Classification.MINOR,
    (ExpectedOutcome.PASS, ExpectedOutcome.FAIL): Classification.FALSE_BLOCK,
    (ExpectedOutcome.NEEDS_REVIEW, ExpectedOutcome.PASS): Classification.DANGEROUS,
    (ExpectedOutcome.NEEDS_REVIEW, ExpectedOutcome.NEEDS_REVIEW): Classification.CORRECT,
    (ExpectedOutcome.NEEDS_REVIEW, ExpectedOutcome.FAIL): Classification.FALSE_BLOCK,
    (ExpectedOutcome.FAIL, ExpectedOutcome.PASS): Classification.DANGEROUS,
    (ExpectedOutcome.FAIL, ExpectedOutcome.NEEDS_REVIEW): Classification.MINOR,
    (ExpectedOutcome.FAIL, ExpectedOutcome.FAIL): Classification.CORRECT,
}
SEVERITY_RANKS = {
    Classification.CORRECT: 0,
    Classification.MINOR: 1,
    Classification.FALSE_BLOCK: 2,
    Classification.DANGEROUS: 3,
}


class ScoredCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    case_id: str
    expected_outcome: ExpectedOutcome
    actual_outcome: ExpectedOutcome
    classification: Classification
    severity_rank: int = Field(ge=0, le=3)
    critical: bool
    tags: list[str]
    response_time_ms: FiniteFloat = Field(ge=0)

    @model_validator(mode="after")
    def require_consistent_classification(self) -> "ScoredCase":
        classification = CLASSIFICATION_MATRIX[self.expected_outcome, self.actual_outcome]
        if self.classification != classification or self.severity_rank != SEVERITY_RANKS[classification]:
            raise ValueError("Classification and severity rank must match the frozen outcome matrix")
        return self


class RunMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    total_cases: int = Field(gt=0)
    correct_cases: int = Field(ge=0)
    minor_cases: int = Field(ge=0)
    false_block_cases: int = Field(ge=0)
    dangerous_cases: int = Field(ge=0)
    critical_dangerous_cases: int = Field(ge=0)
    accuracy: FiniteFloat = Field(ge=0, le=1)
    median_response_time_ms: FiniteFloat = Field(ge=0)
    p95_response_time_ms: FiniteFloat = Field(ge=0)


class ScoredRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    target: TargetName
    run_id: str
    metrics: RunMetrics
    cases: list[ScoredCase]


class RunProblems(BaseModel):
    """Only validation problems; incomplete runs never contain aggregate metrics."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target: TargetName
    run_id: str
    missing_case_ids: list[str] = Field(default_factory=list)
    invalid_case_results: dict[str, str] = Field(default_factory=dict)
    invalid_outcomes: dict[str, str] = Field(default_factory=dict)
    unexpected_canonical_files: list[str] = Field(default_factory=list)
    manifest_errors: list[str] = Field(default_factory=list)
    corpus_errors: list[str] = Field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return any((self.missing_case_ids, self.invalid_case_results, self.invalid_outcomes,
                    self.unexpected_canonical_files, self.manifest_errors, self.corpus_errors))


class RunScoringError(ValueError):
    def __init__(self, problems: RunProblems):
        self.problems = problems
        details = []
        for label, values in (
            ("Manifest problems", problems.manifest_errors),
            ("Corpus problems", problems.corpus_errors),
            ("Missing canonical cases", problems.missing_case_ids),
            ("Unexpected canonical files", problems.unexpected_canonical_files),
        ):
            if values:
                details.append(label + ": " + ", ".join(values))
        for label, values in (
            ("Invalid canonical results", problems.invalid_case_results),
            ("Invalid raw outcomes", problems.invalid_outcomes),
        ):
            if values:
                details.append(label + ": " + "; ".join(f"{key}: {value}" for key, value in values.items()))
        super().__init__("Run is incomplete or invalid. " + " | ".join(details))


def actual_outcome(raw_response: object) -> ExpectedOutcome:
    """Validate only the exact response shape/status, never echoing raw values."""
    if not isinstance(raw_response, dict) or not isinstance(raw_response.get("verification"), dict):
        raise ValueError("raw_response.verification must be an object")
    verification = raw_response["verification"]
    if not isinstance(verification.get("overall"), dict):
        raise ValueError("raw_response.verification.overall must be an object")
    status = verification["overall"].get("status")
    if not isinstance(status, str) or status not in OUTCOME_MAPPING:
        raise ValueError("raw_response.verification.overall.status must be exactly pass, needs_review, or fail")
    return OUTCOME_MAPPING[status]


def score_case(case: Case, result: CaseResult) -> ScoredCase:
    if result.case_id != case.id or result.status != ResultStatus.SUCCESS:
        raise ValueError("Scoring requires the matching canonical successful CaseResult")
    actual = actual_outcome(result.raw_response)
    classification = CLASSIFICATION_MATRIX[case.expected_outcome, actual]
    return ScoredCase(
        case_id=case.id, expected_outcome=case.expected_outcome, actual_outcome=actual,
        classification=classification, severity_rank=SEVERITY_RANKS[classification],
        critical=case.critical, tags=list(case.tags), response_time_ms=result.response_time_ms,
    )


def response_time_statistics(values: list[float]) -> tuple[float, float]:
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Response times must be a nonempty sample of finite nonnegative values")
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return float(median(ordered)), float(ordered[rank - 1])


def aggregate_scores(scores: list[ScoredCase]) -> RunMetrics:
    median_ms, p95_ms = response_time_statistics([score.response_time_ms for score in scores])
    counts = Counter(score.classification for score in scores)
    return RunMetrics(
        total_cases=len(scores), correct_cases=counts[Classification.CORRECT],
        minor_cases=counts[Classification.MINOR], false_block_cases=counts[Classification.FALSE_BLOCK],
        dangerous_cases=counts[Classification.DANGEROUS],
        critical_dangerous_cases=sum(
            score.critical and score.classification == Classification.DANGEROUS for score in scores
        ),
        accuracy=counts[Classification.CORRECT] / len(scores),
        median_response_time_ms=median_ms, p95_response_time_ms=p95_ms,
    )


def _validation_problem(exc: ValidationError) -> str:
    # Validation input can contain provider payloads. Expose error types only.
    return "Invalid structured record (" + ", ".join(sorted({item["type"] for item in exc.errors()})) + ")"


def score_run(
    *, config: GateConfig, target: TargetName, run_id: str,
    cases: list[Case], results_dir: str | Path,
) -> ScoredRun:
    """Read saved results without fingerprint recomputation or verifier access."""
    problems = RunProblems(target=target, run_id=run_id)
    try:
        path = manifest_path(results_dir, target, run_id)
    except ValueError as exc:
        problems.manifest_errors.append(str(exc))
        raise RunScoringError(problems) from exc

    expected = getattr(config.targets, target).expected_fingerprint
    if expected is None:
        problems.manifest_errors.append("Configured target requires a non-null expected_fingerprint")
    if not path.exists() and not path.is_symlink():
        problems.manifest_errors.append("Run manifest.json is required")
    elif not path.is_file() or path.is_symlink():
        problems.manifest_errors.append("Run manifest.json must be a regular file")
    else:
        try:
            manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            problems.manifest_errors.append("Manifest: " + _validation_problem(exc))
        except (OSError, UnicodeError):
            problems.manifest_errors.append("Cannot read run manifest.json")
        else:
            if manifest.run_id != run_id:
                problems.manifest_errors.append("Manifest run_id does not match requested run ID")
            if manifest.target != target:
                problems.manifest_errors.append("Manifest target does not match requested target")
            if expected is not None:
                try:
                    require_fingerprint_match(manifest.fingerprint, expected)
                except ValueError as exc:
                    problems.manifest_errors.append(str(exc))

    if problems.manifest_errors:
        raise RunScoringError(problems)

    if not cases:
        problems.corpus_errors.append("Corpus must contain at least one case")
    known_paths = {}
    for case in sorted(cases, key=lambda case: case.id):
        if case.id in known_paths:
            problems.corpus_errors.append(f"Duplicate case ID: {case.id}")
            continue
        try:
            known_paths[case.id] = result_path(results_dir, target, run_id, case.id)
        except ValueError as exc:
            problems.corpus_errors.append(str(exc))
    if problems.corpus_errors:
        raise RunScoringError(problems)

    if path.parent.exists():
        try:
            allowed = {"manifest.json", *(entry.name for entry in known_paths.values())}
            problems.unexpected_canonical_files = sorted(
                entry.name for entry in path.parent.iterdir()
                if entry.name.endswith(".json") and entry.name not in allowed
            )
        except OSError:
            problems.manifest_errors.append("Cannot inspect run directory")

    scores = []
    for case in sorted(cases, key=lambda case: case.id):
        try:
            result = read_result(results_dir, target, run_id, case.id)
        except ValidationError as exc:
            problems.invalid_case_results[case.id] = _validation_problem(exc)
            continue
        except UnicodeError:
            problems.invalid_case_results[case.id] = "Canonical result must be valid UTF-8 JSON"
            continue
        except (OSError, ValueError) as exc:
            problems.invalid_case_results[case.id] = (
                "Cannot read canonical result" if isinstance(exc, OSError) else str(exc)
            )
            continue
        if result is None:
            problems.missing_case_ids.append(case.id)
            continue
        try:
            scores.append(score_case(case, result))
        except ValidationError as exc:
            problems.invalid_case_results[case.id] = _validation_problem(exc)
        except ValueError as exc:
            problems.invalid_outcomes[case.id] = str(exc)
    if problems.has_problems:
        raise RunScoringError(problems)
    return ScoredRun(target=target, run_id=run_id, metrics=aggregate_scores(scores), cases=scores)
