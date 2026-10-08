"""Final migration decisions using temporary saved evidence, never live runs."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
import yaml

from ai_model_migration_gate import cli, fingerprint, runner, scoring
from ai_model_migration_gate.gate import GateDecision, check_run
from ai_model_migration_gate.results import result_path


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Final check attempted verifier/Git/fingerprint/HTTP work or runtime argparse.error")

    monkeypatch.setattr(cli, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "_git", forbidden)
    monkeypatch.setattr(runner, "preflight_case", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(argparse.ArgumentParser, "error", forbidden)
    original = subprocess.Popen

    def guarded(command, *args, **kwargs):
        assert isinstance(command, list) and command[0] == sys.executable
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)


def mutate(run, case_id, *, target="candidate", run_id=None, status=None, latency=None, critical=None):
    if critical is not None:
        run["cases"] = [case.model_copy(update={"critical": critical}) if case.id == case_id else case
                        for case in run["cases"]]
    path = result_path(run["results_dir"], target, run_id or run["run_id"], case_id)
    data = json.loads(path.read_text())
    if status is not None:
        data["raw_response"]["verification"]["overall"]["status"] = status
    if latency is not None:
        data["response_time_ms"] = latency
    path.write_text(json.dumps(data), encoding="utf-8")


def args(run, *, json_output=True):
    project = run["results_dir"].parent
    (project / "gate.yaml").write_text(yaml.safe_dump(run["config"].model_dump(mode="json")), encoding="utf-8")
    # Keep temporary corpus metadata aligned with any fixture critical-flag edits.
    (project / "cases/cases.jsonl").write_text(
        "".join(case.model_dump_json() + "\n" for case in run["cases"]), encoding="utf-8",
    )
    options = ["check", "--target", run["target"], "--run-id", run["run_id"], "--config", str(project / "gate.yaml")]
    for identity in run["baseline_run_ids"]:
        options += ["--baseline-run-id", identity]
    return options + (["--json"] if json_output else [])


def rules(result):
    return {rule.name: rule for rule in result.rules}


def test_final_pass_scores_all_four_runs_once_in_supplied_order(comparison_runs, monkeypatch):
    original = scoring.score_run
    calls = []

    def observed(**kwargs):
        calls.append((kwargs["target"], kwargs["run_id"]))
        return original(**kwargs)

    monkeypatch.setattr(scoring, "score_run", observed)
    result = check_run(**comparison_runs)
    assert calls == [("candidate", comparison_runs["run_id"])] + [
        ("current", identity) for identity in comparison_runs["baseline_run_ids"]]
    assert result.decision == "PASS" and result.exit_code == 0
    assert result.comparison_required is False
    assert result.baseline_run_ids == comparison_runs["baseline_run_ids"]
    assert result.comparison.baseline_run_ids == tuple(comparison_runs["baseline_run_ids"])
    assert all(rule.status == "pass" for rule in result.rules)
    assert rules(result)["max_dangerous_regressions"].actual == 0


@pytest.mark.parametrize("reference_status", ["fail", "needs_review", "pass"])
def test_stable_correct_minor_false_block_to_dangerous(comparison_runs, reference_status):
    # Needs Review expected: needs_review=correct, fail=false_block. Fail expected: needs_review=minor.
    case_id = "02-wrong-abv" if reference_status == "needs_review" else "09-glare"
    if reference_status == "pass":
        # Cover correct -> dangerous independently.
        case_id, reference_status = "02-wrong-abv", "fail"
    for identity in comparison_runs["baseline_run_ids"]:
        mutate(comparison_runs, case_id, target="current", run_id=identity, status=reference_status)
    mutate(comparison_runs, case_id, status="pass", critical=False)
    result = check_run(**comparison_runs)
    case = next(case for case in result.comparison.cases if case.case_id == case_id)
    assert case.comparison == "got_worse" and case.dangerous_regression
    assert result.metrics.correct_cases == 11 and result.metrics.critical_dangerous_cases == 0
    assert all(rule.status == "pass" for rule in result.rules[:-1])
    assert result.decision == "BLOCKED" and result.exit_code == 1
    assert rules(result)["max_dangerous_regressions"].actual == 1


@pytest.mark.parametrize("count,limit,decision", [(0, 0, "PASS"), (1, 0, "BLOCKED"), (1, 1, "PASS"), (2, 1, "BLOCKED")])
def test_regression_count_and_threshold_boundaries(comparison_runs, count, limit, decision):
    comparison_runs["config"].rules.max_dangerous_regressions = limit
    for case_id in ["02-wrong-abv", "03-wrong-volume"][:count]:
        mutate(comparison_runs, case_id, status="pass", critical=False)
    result = check_run(**comparison_runs)
    assert result.metrics.correct_cases >= 10
    assert result.comparison.metrics.dangerous_regressions == count
    assert result.decision == decision
    assert result.exit_code == (0 if decision == "PASS" else 1)


@pytest.mark.parametrize("critical", [False, True])
def test_variable_baseline_dangerous_counts_candidate_totals_without_regression(comparison_runs, critical):
    identities = comparison_runs["baseline_run_ids"]
    for identity, status in zip(identities, ["fail", "needs_review", "fail"]):
        mutate(comparison_runs, "02-wrong-abv", target="current", run_id=identity, status=status)
    mutate(comparison_runs, "02-wrong-abv", status="pass", critical=critical)
    result = check_run(**comparison_runs)
    case = result.comparison.cases[1]
    assert [value.value for value in case.baseline_classifications] == ["correct", "minor", "correct"]
    assert case.baseline_stability == case.comparison == "baseline_variable"
    assert case.stable_baseline_classification is None and case.dangerous_regression is False
    assert result.comparison.metrics.stable_cases == 11
    assert result.comparison.metrics.baseline_variable_cases == 1
    assert result.comparison.metrics.dangerous_regressions == 0
    assert result.metrics.dangerous_cases == 1 and result.metrics.critical_dangerous_cases == int(critical)
    assert result.decision == ("BLOCKED" if critical else "PASS")
    assert rules(result)["max_dangerous_regressions"].status == "pass"


def test_stable_dangerous_unchanged_is_not_a_regression(comparison_runs):
    for identity in comparison_runs["baseline_run_ids"]:
        mutate(comparison_runs, "02-wrong-abv", target="current", run_id=identity, status="pass")
    mutate(comparison_runs, "02-wrong-abv", status="pass", critical=False)
    result = check_run(**comparison_runs)
    assert result.comparison.cases[1].comparison == "unchanged"
    assert result.comparison.metrics.dangerous_regressions == 0 and result.decision == "PASS"


def test_mixed_comparison_totals_and_reordered_baseline_provenance(comparison_runs):
    for identity, status in zip(comparison_runs["baseline_run_ids"], ["needs_review", "fail", "pass"]):
        mutate(comparison_runs, "09-glare", target="current", run_id=identity, status=status)
    for identity in comparison_runs["baseline_run_ids"]:
        mutate(comparison_runs, "04-brand-case-only", target="current", run_id=identity, status="needs_review")
    mutate(comparison_runs, "01-perfect", status="needs_review")
    mutate(comparison_runs, "02-wrong-abv", status="pass", critical=False)
    result = check_run(**comparison_runs)
    assert result.comparison.metrics.model_dump() == {
        "stable_cases": 11, "baseline_variable_cases": 1, "unchanged_cases": 8,
        "got_better_cases": 1, "got_worse_cases": 2, "dangerous_regressions": 1,
    }
    variable = result.comparison.cases[8]
    assert [value.value for value in variable.baseline_classifications] == ["correct", "false_block", "dangerous"]
    comparison_runs["baseline_run_ids"].reverse()
    reordered = check_run(**comparison_runs)
    assert reordered.comparison.metrics == result.comparison.metrics
    assert reordered.baseline_run_ids == comparison_runs["baseline_run_ids"]
    assert reordered.comparison.baseline_run_ids == tuple(comparison_runs["baseline_run_ids"])
    assert [value.value for value in reordered.comparison.cases[8].baseline_classifications] == [
        "dangerous", "false_block", "correct",
    ]


@pytest.mark.parametrize("failure", ["correct", "critical", "latency", "multiple"])
def test_absolute_failures_and_all_simultaneous_failed_rules(comparison_runs, failure):
    if failure in ("correct", "multiple"):
        for case_id in ["01-perfect", "04-brand-case-only", "10-angled"]:
            mutate(comparison_runs, case_id, status="needs_review")
    if failure in ("critical", "multiple"):
        mutate(comparison_runs, "02-wrong-abv", status="pass")
    if failure in ("latency", "multiple"):
        mutate(comparison_runs, "01-perfect", latency=5000.001)
    result = check_run(**comparison_runs)
    assert result.decision == "BLOCKED" and result.exit_code == 1
    assert len(result.rules) == 4
    if failure == "multiple":
        assert all(rule.status == "fail" for rule in result.rules)


@pytest.mark.parametrize("invalid", ["few", "many", "duplicates", "current_target", "unsafe_baseline",
    "missing_baseline", "incomplete_baseline", "bad_baseline_manifest", "baseline_pin",
    "incomplete_candidate", "bad_candidate_manifest", "candidate_pin", "malformed_outcome", "null_threshold",
    "null_candidate_pin", "null_baseline_pin", "missing_baseline_manifest", "missing_candidate_manifest"])
def test_invalid_final_checks_render_structured_json_and_exit_two(comparison_runs, invalid, capsys):
    run = comparison_runs
    baseline = run["results_dir"] / "current" / run["baseline_run_ids"][0]
    candidate = run["results_dir"] / "candidate" / run["run_id"]
    if invalid == "few":
        run["baseline_run_ids"].pop()
    elif invalid == "many":
        run["baseline_run_ids"].append("fourth")
    elif invalid == "duplicates":
        run["baseline_run_ids"][1] = run["baseline_run_ids"][0]
    elif invalid == "current_target":
        run["target"], run["run_id"] = "current", run["baseline_run_ids"][0]
    elif invalid == "unsafe_baseline":
        run["baseline_run_ids"][0] = "../unsafe"
    elif invalid == "missing_baseline":
        run["baseline_run_ids"][0] = "absent"
    elif invalid in ("incomplete_baseline", "incomplete_candidate"):
        root = baseline if invalid.endswith("baseline") else candidate
        (root / "01-perfect.json").unlink()
    elif invalid in ("bad_baseline_manifest", "bad_candidate_manifest"):
        root = baseline if "baseline" in invalid else candidate
        (root / "manifest.json").write_text("{", encoding="utf-8")
    elif invalid in ("baseline_pin", "candidate_pin"):
        root = baseline if invalid == "baseline_pin" else candidate
        path = root / "manifest.json"; data = json.loads(path.read_text())
        data["fingerprint"]["prompt_sha256"] = "drift"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif invalid == "malformed_outcome":
        mutate(run, "01-perfect", status="UNKNOWN_PRIVATE_VALUE")
    elif invalid in ("null_candidate_pin", "null_baseline_pin"):
        target = "candidate" if invalid == "null_candidate_pin" else "current"
        getattr(run["config"].targets, target).expected_fingerprint = None
    elif invalid in ("missing_baseline_manifest", "missing_candidate_manifest"):
        root = baseline if invalid == "missing_baseline_manifest" else candidate
        (root / "manifest.json").unlink()
    else:
        run["config"].rules.min_correct_cases = None
    assert cli.main(args(run)) == 2
    output = capsys.readouterr()
    assert output.err == "" and "UNKNOWN_PRIVATE_VALUE" not in output.out
    data = json.loads(output.out)
    assert data["decision"] == "INVALID" and data["errors"]
    assert data["baseline_run_ids"] == run["baseline_run_ids"]
    if invalid != "null_threshold":
        assert data["comparison"] is None
    else:
        assert data["metrics"]["correct_cases"] == 12
    if invalid in ("incomplete_candidate", "bad_candidate_manifest", "candidate_pin", "malformed_outcome",
                   "null_candidate_pin", "missing_candidate_manifest"):
        assert data["metrics"] is None


def test_candidate_invalidity_precedes_baseline_semantic_errors(comparison_runs):
    comparison_runs["baseline_run_ids"] = ["one"]
    (comparison_runs["results_dir"] / "candidate" / comparison_runs["run_id"] / "manifest.json").unlink()
    result = check_run(**comparison_runs)
    assert result.metrics is None and result.scoring_problems is not None
    assert "manifest" in result.errors[0].lower()


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("decision,code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
def test_cli_rendering_and_exit_codes(comparison_runs, decision, code, json_output, capsys):
    if decision == "BLOCKED":
        mutate(comparison_runs, "02-wrong-abv", status="pass", critical=False)
    elif decision == "INVALID":
        comparison_runs["baseline_run_ids"].pop()
    assert cli.main(args(comparison_runs, json_output=json_output)) == code
    output = capsys.readouterr()
    assert output.err == ""
    if json_output:
        result = GateDecision.model_validate_json(output.out)
        assert result.exit_code == code and result.decision == decision
        assert result.baseline_run_ids == comparison_runs["baseline_run_ids"]
        if decision != "INVALID":
            assert result.comparison_required is False
            assert result.comparison.baseline_run_ids == tuple(comparison_runs["baseline_run_ids"])
            assert len(result.comparison.cases) == 12
        assert "raw_response" not in output.out
    else:
        assert output.out.startswith(decision + ":")
        if decision != "INVALID":
            assert "stable=12; baseline_variable=0" in output.out
            assert "dangerous_regressions=" in output.out and "got_better=" in output.out
            assert "Current baseline runs: baseline-z / baseline-a / baseline-m" in output.out


@pytest.mark.parametrize("decision,code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
def test_final_actual_process_exit_with_external_work_forbidden(comparison_runs, decision, code):
    if decision == "BLOCKED":
        mutate(comparison_runs, "02-wrong-abv", status="pass", critical=False)
    elif decision == "INVALID":
        comparison_runs["baseline_run_ids"].pop()
    script = '''
import socket, subprocess, httpx
from ai_model_migration_gate import fingerprint, runner, cli
def forbidden(*a, **kw):
    raise RuntimeError("External work attempted")
socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = forbidden
httpx.Client = fingerprint.calculate_fingerprint = fingerprint._git = forbidden
runner.preflight_case = subprocess.Popen = forbidden
raise SystemExit(cli.main())
'''
    result = subprocess.run([sys.executable, "-c", script, *args(comparison_runs)],
                            capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == code and result.stderr == ""
    assert json.loads(result.stdout)["decision"] == decision


def test_relative_paths_and_read_only_four_run_access(comparison_runs, tmp_path, monkeypatch, capsys):
    options = args(comparison_runs)
    project = comparison_runs["results_dir"].parent
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in project.rglob('*') if p.is_file()}
    original = Path.open

    def guard(path, mode="r", *a, **kw):
        assert path in before and not any(flag in mode for flag in ("w", "a", "x", "+"))
        return original(path, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", guard)
    monkeypatch.chdir(tmp_path)
    assert cli.main(options) == 0
    assert json.loads(capsys.readouterr().out)["comparison_required"] is False
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before


def test_single_candidate_check_still_defers_comparison(comparison_runs):
    run = {key: value for key, value in comparison_runs.items() if key != "baseline_run_ids"}
    result = check_run(**run)
    assert result.decision == "PASS" and result.comparison_required is True
    assert rules(result)["max_dangerous_regressions"].status == "not_evaluated"
    assert result.comparison is None
