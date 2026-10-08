"""Offline absolute rules and final three-baseline migration decisions."""

from enum import Enum
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from . import scoring
from .cases import Case
from .config import GateConfig, GateRules
from .comparison import RunComparison, compare_runs
from .results import TargetName
from .scoring import RunMetrics, RunProblems, RunScoringError, ScoredRun


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
    comparison_required: bool = True
    baseline_run_ids: list[str] = Field(default_factory=list)
    comparison: RunComparison | None = None
    errors: list[str] = Field(default_factory=list)
    scoring_problems: RunProblems | None = None

    @model_validator(mode="after")
    def require_evaluated_final_comparison(self) -> "GateDecision":
        if self.decision != Decision.INVALID and not self.comparison_required:
            if self.comparison is None or self.baseline_run_ids != list(self.comparison.baseline_run_ids):
                raise ValueError("Final decisions require the comparison and its ordered baseline IDs")
            regression_rules = [rule for rule in self.rules if rule.name == "max_dangerous_regressions"]
            if len(regression_rules) != 1 or regression_rules[0].status == RuleStatus.NOT_EVALUATED:
                raise ValueError("Final decisions must evaluate dangerous regressions")
        return self

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
    baseline_run_ids: list[str] | None = None,
) -> GateDecision:
    return GateDecision(
        target=target, run_id=run_id, decision=Decision.INVALID, metrics=metrics,
        rules=rules if rules is not None else [_comparison_rule()],
        errors=[message], scoring_problems=scoring_problems,
        baseline_run_ids=baseline_run_ids or [],
        comparison_required=baseline_run_ids is None,
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
    baseline_run_ids: list[str] | None = None,
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
            baseline_run_ids=baseline_run_ids,
        )
    except (OSError, ValueError):
        return invalid_decision(
            target=target, run_id=run_id, message="Cannot validate or read saved run inputs",
            rules=_absolute_rules(None, config.rules, unevaluated_reason="Run scoring is invalid or incomplete")
                  + [comparison],
            baseline_run_ids=baseline_run_ids,
        )

    if baseline_run_ids is None:
        return _evaluate_scored_run(report, config.rules)

    def invalid_comparison(message: str, problems: RunProblems | None = None) -> GateDecision:
        return invalid_decision(
            target=target, run_id=run_id, message=message, metrics=report.metrics,
            scoring_problems=problems, baseline_run_ids=baseline_run_ids,
            rules=_absolute_rules(report.metrics, config.rules, unevaluated_reason="Comparison inputs are invalid")
                  + [comparison],
        )

    if target != "candidate":
        return invalid_comparison("Baseline comparison requires target candidate")
    if len(baseline_run_ids) != 3 or len(set(baseline_run_ids)) != 3:
        return invalid_comparison("Comparison requires exactly three distinct baseline run IDs")
    baselines = []
    for baseline_id in baseline_run_ids:
        try:
            baselines.append(scoring.score_run(
                config=config, target="current", run_id=baseline_id, cases=cases, results_dir=results_dir,
            ))
        except RunScoringError as exc:
            return invalid_comparison(f"Baseline {baseline_id}: {exc}", exc.problems)
        except (OSError, ValueError):
            return invalid_comparison(f"Cannot validate or read baseline run {baseline_id}")
    try:
        comparison_report = compare_runs(baselines=baselines, candidate=report)
    except ValueError as exc:
        return invalid_comparison(str(exc))

    absolute = _evaluate_scored_run(report, config.rules)
    if absolute.decision == Decision.INVALID:
        return absolute.model_copy(update={
            "baseline_run_ids": list(baseline_run_ids), "comparison_required": False,
            "comparison": comparison_report,
        })
    regressions = comparison_report.metrics.dangerous_regressions
    regression_rule = RuleEvaluation(
        name="max_dangerous_regressions",
        status=RuleStatus.PASS if regressions <= config.rules.max_dangerous_regressions else RuleStatus.FAIL,
        actual=regressions, threshold=config.rules.max_dangerous_regressions,
        message="Stable current cases becoming candidate dangerous must be at most the maximum",
    )
    rules = absolute.rules[:-1] + [regression_rule]
    return GateDecision(
        target=target, run_id=run_id, metrics=report.metrics, rules=rules,
        decision=Decision.BLOCKED if any(rule.status == RuleStatus.FAIL for rule in rules) else Decision.PASS,
        comparison_required=False, baseline_run_ids=list(baseline_run_ids), comparison=comparison_report,
    )


def _evaluate_scored_run(report: ScoredRun, policy: GateRules) -> GateDecision:
    """Shared absolute evaluator for single-run and final candidate decisions."""
    target, run_id = report.target, report.run_id
    comparison = _comparison_rule(policy.max_dangerous_regressions)
    policy_errors = []
    if policy.min_correct_cases is None:
        policy_errors.append("min_correct_cases is null: the required release policy has not been frozen")
    if not math.isfinite(policy.max_slow_case_seconds * 1000):
        policy_errors.append("max_slow_case_seconds must produce a finite millisecond limit")
    if policy_errors:
        return invalid_decision(
            target=target, run_id=run_id, message="; ".join(policy_errors), metrics=report.metrics,
            rules=_absolute_rules(report.metrics, policy, unevaluated_reason="Required absolute policy is invalid or unfrozen")
                  + [comparison],
        )

    absolute_rules = _absolute_rules(report.metrics, policy)
    decision = Decision.BLOCKED if any(rule.status == RuleStatus.FAIL for rule in absolute_rules) else Decision.PASS
    return GateDecision(
        target=target, run_id=run_id, decision=decision, metrics=report.metrics,
        rules=absolute_rules + [comparison],
    )
