"""Absolute single-run release rules; migration comparison remains deferred."""

from enum import Enum
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from . import scoring
from .cases import Case
from .config import GateConfig, GateRules
from .results import TargetName
from .scoring import RunMetrics, RunProblems, RunScoringError


class Decision(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"


class RuleStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"


class RuleEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: Literal["max_critical_dangerous_mistakes", "min_correct_cases",
                  "max_slow_case_seconds", "max_dangerous_regressions"]
    status: RuleStatus
    actual: int | FiniteFloat | None
    threshold: int | FiniteFloat | None
    message: str


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    target: TargetName
    run_id: str
    decision: Decision
    metrics: RunMetrics | None = None
    rules: list[RuleEvaluation] = Field(default_factory=list)
    comparison_required: Literal[True] = True
    errors: list[str] = Field(default_factory=list)
    scoring_problems: RunProblems | None = None

    @property
    def exit_code(self) -> int:
        return {Decision.PASS: 0, Decision.BLOCKED: 1, Decision.INVALID: 2}[self.decision]


def _comparison_rule(threshold: int | None = None) -> RuleEvaluation:
    return RuleEvaluation(
        name="max_dangerous_regressions", status=RuleStatus.NOT_EVALUATED,
        actual=None, threshold=threshold,
        message="Dangerous-regression comparison is not evaluated; current-vs-candidate comparison is still required",
    )


def invalid_decision(
    *, target: TargetName, run_id: str, message: str,
    metrics: RunMetrics | None = None, rules: list[RuleEvaluation] | None = None,
    scoring_problems: RunProblems | None = None,
) -> GateDecision:
    return GateDecision(
        target=target, run_id=run_id, decision=Decision.INVALID, metrics=metrics,
        rules=rules if rules is not None else [_comparison_rule()],
        errors=[message], scoring_problems=scoring_problems,
    )


def _absolute_rules(
    metrics: RunMetrics | None, policy: GateRules, *, unevaluated_reason: str | None = None,
) -> list[RuleEvaluation]:
    latency_ms = policy.max_slow_case_seconds * 1000
    specifications = [
        ("max_critical_dangerous_mistakes", metrics.critical_dangerous_cases if metrics else None,
         policy.max_critical_dangerous_mistakes, "Critical dangerous cases must be at most the maximum", False),
        ("min_correct_cases", metrics.correct_cases if metrics else None,
         policy.min_correct_cases, "Correct cases must meet the configured minimum", True),
        ("max_slow_case_seconds", metrics.p95_response_time_ms if metrics else None,
         latency_ms if math.isfinite(latency_ms) else None,
         "Nearest-rank p95 client response time (ms) must be at most max_slow_case_seconds * 1000", False),
    ]
    evaluations = []
    for name, actual, threshold, message, minimum in specifications:
        if unevaluated_reason is not None:
            status = RuleStatus.NOT_EVALUATED
            message = unevaluated_reason
        else:
            passed = actual >= threshold if minimum else actual <= threshold
            status = RuleStatus.PASS if passed else RuleStatus.FAIL
        evaluations.append(RuleEvaluation(
            name=name, status=status, actual=actual, threshold=threshold, message=message,
        ))
    return evaluations


def check_run(
    *, config: GateConfig, target: TargetName, run_id: str,
    cases: list[Case], results_dir: str | Path,
) -> GateDecision:
    """Scoring validation precedes policy validation and absolute comparisons."""
    comparison = _comparison_rule(config.rules.max_dangerous_regressions)
    try:
        report = scoring.score_run(
            config=config, target=target, run_id=run_id, cases=cases, results_dir=results_dir,
        )
    except RunScoringError as exc:
        return invalid_decision(
            target=target, run_id=run_id, message=str(exc), scoring_problems=exc.problems,
            rules=_absolute_rules(None, config.rules, unevaluated_reason="Run scoring is invalid or incomplete")
                  + [comparison],
        )
    except (OSError, ValueError):
        return invalid_decision(
            target=target, run_id=run_id, message="Cannot validate or read saved run inputs",
            rules=_absolute_rules(None, config.rules, unevaluated_reason="Run scoring is invalid or incomplete")
                  + [comparison],
        )

    policy_errors = []
    if config.rules.min_correct_cases is None:
        policy_errors.append("min_correct_cases is null: the required release policy has not been frozen")
    if not math.isfinite(config.rules.max_slow_case_seconds * 1000):
        policy_errors.append("max_slow_case_seconds must produce a finite millisecond limit")
    if policy_errors:
        return invalid_decision(
            target=target, run_id=run_id, message="; ".join(policy_errors), metrics=report.metrics,
            rules=_absolute_rules(report.metrics, config.rules, unevaluated_reason="Required absolute policy is invalid or unfrozen")
                  + [comparison],
        )

    absolute_rules = _absolute_rules(report.metrics, config.rules)
    decision = Decision.BLOCKED if any(rule.status == RuleStatus.FAIL for rule in absolute_rules) else Decision.PASS
    return GateDecision(
        target=target, run_id=run_id, decision=decision, metrics=report.metrics,
        rules=absolute_rules + [comparison],
    )
