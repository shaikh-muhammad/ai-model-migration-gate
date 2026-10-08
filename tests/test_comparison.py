"""Frozen stability and severity comparisons from validated scorer outputs."""

import itertools

import pytest
from pydantic import ValidationError

from ai_model_migration_gate.comparison import (
    BaselineStability, CandidateComparison, CaseComparison, ComparisonMetrics,
    RunComparison, classify_comparison, compare_runs,
)
from ai_model_migration_gate.scoring import Classification, score_run


def reports(run):
    inputs = {key: run[key] for key in ("config", "cases", "results_dir")}
    baselines = [score_run(**inputs, target="current", run_id=value) for value in run["baseline_run_ids"]]
    candidate = score_run(**inputs, target="candidate", run_id=run["run_id"])
    return baselines, candidate


def test_exact_enums():
    assert [item.value for item in BaselineStability] == ["stable", "baseline_variable"]
    assert [item.value for item in CandidateComparison] == ["unchanged", "got_better", "got_worse", "baseline_variable"]


@pytest.mark.parametrize("baseline,candidate", list(itertools.product(Classification, repeat=2)))
def test_all_stable_severity_comparisons(baseline, candidate):
    # Independently frozen ordering, including dangerous -> dangerous unchanged.
    ranks = {"correct": 0, "minor": 1, "false_block": 2, "dangerous": 3}
    delta = ranks[candidate.value] - ranks[baseline.value]
    expected = "unchanged" if delta == 0 else "got_better" if delta < 0 else "got_worse"
    stability, reference, comparison, regression = classify_comparison((baseline,) * 3, candidate)
    assert stability == BaselineStability.STABLE and reference == baseline
    assert comparison.value == expected
    assert regression is (expected == "got_worse" and candidate == Classification.DANGEROUS)


@pytest.mark.parametrize("baseline", [values for values in itertools.product(Classification, repeat=3)
                                     if len(set(values)) != 1])
def test_every_mixed_baseline_is_variable_without_a_reference_or_regression(baseline):
    for candidate in Classification:
        assert classify_comparison(baseline, candidate) == (
            BaselineStability.BASELINE_VARIABLE, None, CandidateComparison.BASELINE_VARIABLE, False,
        )


@pytest.mark.parametrize("length", [0, 1, 2, 4])
def test_stability_requires_three_observations(length):
    with pytest.raises(ValueError, match="exactly three"):
        classify_comparison((Classification.CORRECT,) * length, Classification.CORRECT)


def test_scorer_outputs_only_with_canonical_order_and_supplied_baseline_order(comparison_runs, monkeypatch):
    baselines, candidate = reports(comparison_runs)
    for report in [*baselines, candidate]:
        report.cases.reverse()

    def forbidden(*args, **kwargs):
        pytest.fail("Comparison attempted to read files instead of using scorer outputs")

    monkeypatch.setattr("pathlib.Path.open", forbidden)
    result = compare_runs(baselines=baselines, candidate=candidate)
    assert result.baseline_run_ids == tuple(comparison_runs["baseline_run_ids"])
    assert [case.case_id for case in result.cases] == sorted(case.case_id for case in candidate.cases)
    assert result.metrics == ComparisonMetrics(
        stable_cases=12, baseline_variable_cases=0, unchanged_cases=12,
        got_better_cases=0, got_worse_cases=0, dangerous_regressions=0,
    )
    assert RunComparison.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("invalid", ["count", "duplicate", "target", "candidate_target", "case_ids", "duplicate_cases", "metadata"])
def test_incompatible_scorer_outputs_are_rejected(comparison_runs, invalid):
    baselines, candidate = reports(comparison_runs)
    if invalid == "count":
        baselines.pop()
    elif invalid == "duplicate":
        baselines[1] = baselines[0]
    elif invalid == "target":
        baselines[0] = baselines[0].model_copy(update={"target": "candidate"})
    elif invalid == "candidate_target":
        candidate = candidate.model_copy(update={"target": "current"})
    elif invalid == "case_ids":
        baselines[0].cases.pop()
    elif invalid == "duplicate_cases":
        baselines[0].cases.append(baselines[0].cases[0])
    else:
        baselines[0].cases[0] = baselines[0].cases[0].model_copy(update={"critical": True})
    with pytest.raises(ValueError):
        compare_runs(baselines=baselines, candidate=candidate)


def test_models_reject_extra_fields_wrong_types_and_inconsistent_comparisons(comparison_runs):
    baselines, candidate = reports(comparison_runs)
    result = compare_runs(baselines=baselines, candidate=candidate)
    for model in [result, result.metrics, result.cases[0]]:
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "extra": "forbidden"})
    data = result.cases[0].model_dump()
    with pytest.raises(ValidationError):
        CaseComparison.model_validate({**data, "critical": "false"})
    with pytest.raises(ValidationError, match="Comparison must match"):
        CaseComparison.model_validate({**data, "dangerous_regression": True})
