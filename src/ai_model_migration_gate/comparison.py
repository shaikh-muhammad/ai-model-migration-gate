"""Compare validated scorer outputs without reading results or verifier files."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cases import ExpectedOutcome
from .scoring import Classification, SEVERITY_RANKS, ScoredRun


class BaselineStability(str, Enum):
    STABLE = "stable"
    BASELINE_VARIABLE = "baseline_variable"


class CandidateComparison(str, Enum):
    UNCHANGED = "unchanged"
    GOT_BETTER = "got_better"
    GOT_WORSE = "got_worse"
    BASELINE_VARIABLE = "baseline_variable"


def classify_comparison(
    baseline: tuple[Classification, Classification, Classification],
    candidate: Classification,
) -> tuple[BaselineStability, Classification | None, CandidateComparison, bool]:
    if len(baseline) != 3:
        raise ValueError("Stability requires exactly three baseline classifications")
    if len(set(baseline)) != 1:
        return (BaselineStability.BASELINE_VARIABLE, None,
                CandidateComparison.BASELINE_VARIABLE, False)
    reference = baseline[0]
    delta = SEVERITY_RANKS[candidate] - SEVERITY_RANKS[reference]
    comparison = (CandidateComparison.UNCHANGED if delta == 0 else
                  CandidateComparison.GOT_BETTER if delta < 0 else CandidateComparison.GOT_WORSE)
    regression = comparison == CandidateComparison.GOT_WORSE and candidate == Classification.DANGEROUS
    return BaselineStability.STABLE, reference, comparison, regression


class CaseComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    case_id: str
    critical: bool
    baseline_classifications: tuple[Classification, Classification, Classification]
    baseline_stability: BaselineStability
    stable_baseline_classification: Classification | None
    candidate_classification: Classification
    candidate_actual_outcome: ExpectedOutcome
    comparison: CandidateComparison
    dangerous_regression: bool

    @model_validator(mode="after")
    def require_consistent_comparison(self) -> "CaseComparison":
        expected = classify_comparison(self.baseline_classifications, self.candidate_classification)
        actual = (self.baseline_stability, self.stable_baseline_classification,
                  self.comparison, self.dangerous_regression)
        if actual != expected:
            raise ValueError("Comparison must match three-run stability and existing severity ranks")
        return self


class ComparisonMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    stable_cases: int = Field(ge=0)
    baseline_variable_cases: int = Field(ge=0)
    unchanged_cases: int = Field(ge=0)
    got_better_cases: int = Field(ge=0)
    got_worse_cases: int = Field(ge=0)
    dangerous_regressions: int = Field(ge=0)


class RunComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    baseline_run_ids: tuple[str, str, str]
    candidate_run_id: str
    metrics: ComparisonMetrics
    cases: list[CaseComparison]


def compare_runs(*, baselines: list[ScoredRun], candidate: ScoredRun) -> RunComparison:
    """Use only four complete scorer outputs, retaining baseline input order."""
    if len(baselines) != 3 or len({run.run_id for run in baselines}) != 3:
        raise ValueError("Comparison requires exactly three distinct baseline run IDs")
    if candidate.target != "candidate" or any(run.target != "current" for run in baselines):
        raise ValueError("Comparison requires a candidate run and three current baseline runs")
    indexes = [{case.case_id: case for case in run.cases} for run in [*baselines, candidate]]
    if any(len(index) != len(run.cases) for index, run in zip(indexes, [*baselines, candidate])):
        raise ValueError("Comparison requires unique case IDs")
    if not indexes[-1] or any(index.keys() != indexes[-1].keys() for index in indexes):
        raise ValueError("Comparison runs must contain the same complete case IDs")
    cases = []
    for case_id in sorted(indexes[-1]):
        current = [index[case_id] for index in indexes[:3]]
        proposed = indexes[-1][case_id]
        if any((case.expected_outcome, case.critical, case.tags) !=
               (proposed.expected_outcome, proposed.critical, proposed.tags) for case in current):
            raise ValueError("Comparison runs must use the same case expectations and metadata")
        classifications = tuple(case.classification for case in current)
        stability, reference, comparison, regression = classify_comparison(
            classifications, proposed.classification,
        )
        cases.append(CaseComparison(
            case_id=case_id, critical=proposed.critical,
            baseline_classifications=classifications, baseline_stability=stability,
            stable_baseline_classification=reference, candidate_classification=proposed.classification,
            candidate_actual_outcome=proposed.actual_outcome, comparison=comparison,
            dangerous_regression=regression,
        ))
    return RunComparison(
        baseline_run_ids=tuple(run.run_id for run in baselines), candidate_run_id=candidate.run_id,
        metrics=ComparisonMetrics(
            stable_cases=sum(case.baseline_stability == BaselineStability.STABLE for case in cases),
            baseline_variable_cases=sum(case.baseline_stability == BaselineStability.BASELINE_VARIABLE for case in cases),
            unchanged_cases=sum(case.comparison == CandidateComparison.UNCHANGED for case in cases),
            got_better_cases=sum(case.comparison == CandidateComparison.GOT_BETTER for case in cases),
            got_worse_cases=sum(case.comparison == CandidateComparison.GOT_WORSE for case in cases),
            dangerous_regressions=sum(case.dangerous_regression for case in cases),
        ), cases=cases,
    )
