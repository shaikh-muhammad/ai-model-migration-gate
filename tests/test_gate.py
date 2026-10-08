"""Frozen absolute rule boundaries evaluated from temporary saved runs."""

import json
from pathlib import Path
import subprocess

import httpx
import pytest
from pydantic import ValidationError

from ai_model_migration_gate import fingerprint, gate, runner, scoring
from ai_model_migration_gate.cases import ExpectedOutcome
from ai_model_migration_gate.gate import Decision, GateDecision, RuleEvaluation, RuleStatus, check_run
from ai_model_migration_gate.manifest import manifest_path
from ai_model_migration_gate.results import result_path


@pytest.fixture(autouse=True)
def forbid_gate_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Single-run check attempted verifier, fingerprint, Git, or client work")

    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "_git", forbidden)
    monkeypatch.setattr(runner, "preflight_case", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)


@pytest.fixture
def frozen_run(saved_run):
    saved_run["config"].rules.min_correct_cases = 11
    return saved_run


def change_result(run, case, *, raw_status=None, response_time_ms=None):
    path = result_path(run["results_dir"], run["target"], run["run_id"], case.id)
    data = json.loads(path.read_text())
    if raw_status is not None:
        data["raw_response"]["verification"]["overall"]["status"] = raw_status
    if response_time_ms is not None:
        data["response_time_ms"] = response_time_ms
    path.write_text(json.dumps(data), encoding="utf-8")


def rule_map(result):
    return {rule.name: rule for rule in result.rules}


def test_decision_and_rule_status_enums_have_exact_values():
    assert [decision.value for decision in Decision] == ["PASS", "BLOCKED", "INVALID"]
    assert [status.value for status in RuleStatus] == ["pass", "fail", "not_evaluated"]


def test_null_policy_is_invalid_after_successful_scoring(saved_run):
    saved_run["config"].rules.min_correct_cases = None
    result = check_run(**saved_run)
    assert result.decision == Decision.INVALID
    assert result.exit_code == 2
    assert result.metrics.total_cases == result.metrics.correct_cases == 12
    assert result.scoring_problems is None
    assert "min_correct_cases is null" in result.errors[0]
    assert all(rule.status == RuleStatus.NOT_EVALUATED for rule in result.rules)
    assert rule_map(result)["min_correct_cases"].threshold is None
    assert result.comparison_required is True


@pytest.mark.parametrize("correct_count, status, decision", [
    (11, RuleStatus.PASS, Decision.PASS), (10, RuleStatus.FAIL, Decision.BLOCKED),
])
def test_correct_count_boundary_from_complete_run(frozen_run, correct_count, status, decision):
    for case in frozen_run["cases"][:12 - correct_count]:
        # Any wrong response here avoids inventing a dangerous mistake.
        raw = "fail" if case.expected_outcome == ExpectedOutcome.NEEDS_REVIEW else "needs_review"
        change_result(frozen_run, case, raw_status=raw)
    result = check_run(**frozen_run)
    rules = rule_map(result)
    assert result.metrics.correct_cases == correct_count
    assert rules["min_correct_cases"].actual == correct_count
    assert rules["min_correct_cases"].threshold == 11
    assert rules["min_correct_cases"].status == status
    assert rules["max_critical_dangerous_mistakes"].status == RuleStatus.PASS
    assert rules["max_slow_case_seconds"].status == RuleStatus.PASS
    assert result.decision == decision


@pytest.mark.parametrize("dangerous_count, status, decision", [
    (0, RuleStatus.PASS, Decision.PASS), (1, RuleStatus.FAIL, Decision.BLOCKED),
])
def test_critical_dangerous_boundary_from_complete_run(frozen_run, dangerous_count, status, decision):
    if dangerous_count:
        case = next(case for case in frozen_run["cases"] if case.critical and case.expected_outcome != ExpectedOutcome.PASS)
        change_result(frozen_run, case, raw_status="pass")
    result = check_run(**frozen_run)
    rules = rule_map(result)
    assert result.metrics.critical_dangerous_cases == dangerous_count
    assert rules["max_critical_dangerous_mistakes"].actual == dangerous_count
    assert rules["max_critical_dangerous_mistakes"].threshold == 0
    assert rules["max_critical_dangerous_mistakes"].status == status
    assert rules["min_correct_cases"].status == RuleStatus.PASS
    assert rules["max_slow_case_seconds"].status == RuleStatus.PASS
    assert result.decision == decision


@pytest.mark.parametrize("p95, status, decision", [
    (5000.0, RuleStatus.PASS, Decision.PASS), (5000.001, RuleStatus.FAIL, Decision.BLOCKED),
])
def test_unrounded_millisecond_latency_boundary(frozen_run, p95, status, decision):
    change_result(frozen_run, frozen_run["cases"][-1], response_time_ms=p95)
    result = check_run(**frozen_run)
    rules = rule_map(result)
    assert result.metrics.p95_response_time_ms == p95
    assert rules["max_slow_case_seconds"].actual == p95
    assert rules["max_slow_case_seconds"].threshold == 5000.0
    assert rules["max_slow_case_seconds"].status == status
    assert rules["min_correct_cases"].status == RuleStatus.PASS
    assert rules["max_critical_dangerous_mistakes"].status == RuleStatus.PASS
    assert result.decision == decision


def test_multiple_absolute_failures_are_blocked(frozen_run):
    for case in frozen_run["cases"]:
        change_result(frozen_run, case, raw_status="pass", response_time_ms=6000.0)
    result = check_run(**frozen_run)
    assert result.decision == Decision.BLOCKED
    assert result.exit_code == 1
    assert [rule.status for rule in result.rules[:3]] == [RuleStatus.FAIL] * 3


def test_absolute_pass_is_not_final_migration_approval(frozen_run):
    result = check_run(**frozen_run)
    assert result.decision == Decision.PASS and result.exit_code == 0
    assert all(rule.status == RuleStatus.PASS for rule in result.rules[:3])
    comparison = rule_map(result)["max_dangerous_regressions"]
    assert comparison.status == RuleStatus.NOT_EVALUATED
    assert comparison.actual is None
    assert comparison.threshold == 0
    assert "still required" in comparison.message
    assert result.comparison_required is True
    assert GateDecision.model_validate_json(result.model_dump_json()) == result


def test_noncritical_dangerous_case_does_not_invent_regression_failures(frozen_run):
    index = next(index for index, case in enumerate(frozen_run["cases"]) if case.expected_outcome != ExpectedOutcome.PASS)
    case = frozen_run["cases"][index].model_copy(update={"critical": False})
    frozen_run["cases"][index] = case
    change_result(frozen_run, case, raw_status="pass")
    result = check_run(**frozen_run)
    assert result.metrics.dangerous_cases == 1
    assert result.metrics.critical_dangerous_cases == 0
    assert result.decision == Decision.PASS
    assert rule_map(result)["max_dangerous_regressions"].status == RuleStatus.NOT_EVALUATED


def test_null_policy_precedes_other_absolute_failures(saved_run):
    saved_run["config"].rules.min_correct_cases = None
    for case in saved_run["cases"]:
        change_result(saved_run, case, raw_status="pass", response_time_ms=6000.0)
    result = check_run(**saved_run)
    assert result.decision == Decision.INVALID and result.exit_code == 2
    assert result.metrics.critical_dangerous_cases > 0
    assert result.metrics.p95_response_time_ms > 5000
    assert all(rule.status == RuleStatus.NOT_EVALUATED for rule in result.rules)


def test_structural_invalidity_precedes_null_policy_and_has_no_metrics(saved_run):
    manifest_path(saved_run["results_dir"], "current", saved_run["run_id"]).unlink()
    result = check_run(**saved_run)
    assert result.decision == Decision.INVALID and result.exit_code == 2
    assert result.metrics is None
    assert result.scoring_problems.manifest_errors == ["Run manifest.json is required"]
    assert "policy has not been frozen" not in result.errors[0]
    assert all(rule.actual is None for rule in result.rules)
    assert all(rule.status == RuleStatus.NOT_EVALUATED for rule in result.rules)


def test_scorer_is_called_once_and_is_the_only_source_of_metrics(frozen_run, monkeypatch):
    original = scoring.score_run
    reports = []

    def observe(**kwargs):
        assert kwargs == frozen_run
        report = original(**kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(scoring, "score_run", observe)
    result = check_run(**frozen_run)
    assert len(reports) == 1
    assert result.metrics == reports[0].metrics
    assert result.metrics.median_response_time_ms == 650.0
    assert result.metrics.p95_response_time_ms == 1200.0


@pytest.mark.parametrize("field, value", [("status", "pass"), ("actual", "12"), ("threshold", True),
                                         ("extra", "unused"), ("name", "made_up_rule")])
def test_rule_evaluation_model_is_strict(frozen_run, field, value):
    data = check_run(**frozen_run).rules[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        RuleEvaluation.model_validate(data)


def test_decision_model_rejects_extra_fields_and_unknown_decisions(frozen_run):
    data = check_run(**frozen_run).model_dump()
    for changes in ({"extra": "unused"}, {"decision": "PASS"}, {"comparison_required": False}):
        with pytest.raises(ValidationError):
            GateDecision.model_validate({**data, **changes})


@pytest.mark.parametrize("limit", [float("inf"), 1e308])
def test_nonfinite_latency_policy_is_invalid_without_fabricating_a_limit(frozen_run, limit):
    frozen_run["config"].rules.max_slow_case_seconds = limit
    result = check_run(**frozen_run)
    assert result.decision == Decision.INVALID
    assert result.metrics is not None
    assert rule_map(result)["max_slow_case_seconds"].threshold is None
    assert "finite millisecond limit" in result.errors[0]


def test_gate_evaluation_is_read_only_and_never_inspects_images_or_verifier(frozen_run, monkeypatch):
    root = manifest_path(frozen_run["results_dir"], "current", frozen_run["run_id"]).parent
    files = set(root.iterdir())
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    original_open, original_stat = Path.open, Path.stat

    def guard_open(path, mode="r", *args, **kwargs):
        assert path in files
        assert not any(flag in mode for flag in ("w", "a", "x", "+"))
        return original_open(path, mode, *args, **kwargs)

    def guard_stat(path, *args, **kwargs):
        assert "absent-verifier" not in path.parts and path.suffix != ".png"
        return original_stat(path, *args, **kwargs)

    def forbidden_write(*args, **kwargs):
        pytest.fail("Gate evaluation wrote a file")

    monkeypatch.setattr(Path, "open", guard_open)
    monkeypatch.setattr(Path, "stat", guard_stat)
    monkeypatch.setattr(Path, "mkdir", forbidden_write)
    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    assert check_run(**frozen_run).decision == Decision.PASS
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before
