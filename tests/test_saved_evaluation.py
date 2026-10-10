"""Helper modes and selectors; synthetic experiments exist only in tmp_path."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ai_model_migration_gate import fingerprint
from ai_model_migration_gate.gate import Decision, RuleStatus
from ai_model_migration_gate.manifest import RunManifest, create_manifest
from ai_model_migration_gate.results import CaseResult, write_result
from test_config_invariants import ROOT, candidate_config, evaluation, save_config, saved_project


@pytest.fixture
def release_project(saved_project):
    root = saved_project
    save_config(root, candidate_config(root), "gate.pass.yaml")
    configs, cases = evaluation.validate_configs(root)
    run_id = "offline-fixture-candidate"
    create_manifest(root / "results", RunManifest(
        run_id=run_id, target="candidate", created_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
        fingerprint=configs["gate.pass.yaml"].targets.candidate.expected_fingerprint,
    ))
    statuses = {"Pass": "pass", "Fail": "fail", "Needs Review": "needs_review"}
    for case in cases:
        write_result(root / "results", run_id, CaseResult(
            case_id=case.id, target="candidate", status="success", http_status=200, response_time_ms=100.0,
            raw_response={"verification": {"overall": {"status": statuses[case.expected_outcome.value]}}},
        ))
    (root / "release.json").write_text(json.dumps({"config": "gate.pass.yaml", "run_id": run_id}))
    record_observation(root)
    return root


def record_observation(root, *, kind="official"):
    """Register a fabricated observation only within the isolated test project."""
    selector = json.loads((root / "release.json").read_text())
    configs, cases = evaluation.validate_configs(root)
    observed = evaluation.evaluate(root, configs[selector["config"]], cases, selector["run_id"])
    entry = {**selector, "kind": kind,
             "evidence_sha256": evaluation.evidence_digest(root, "candidate", selector["run_id"]),
             "expected": observed.model_dump(mode="json")}
    (root / "experiments.json").write_text(json.dumps({"experiments": [entry]}))


def mutate_result(root, run_id, case_id, *, status=None):
    path = root / "results/candidate" / run_id / f"{case_id}.json"
    data = json.loads(path.read_text())
    if status is not None:
        data["raw_response"]["verification"]["overall"]["status"] = status
    path.write_text(json.dumps(data))


def assert_invalid(root, capsys):
    assert evaluation.main(["release"], root=root) == 2
    output = capsys.readouterr()
    assert "INVALID" in output.out + output.err
    assert "Traceback" not in output.err


def test_actual_luna_historical_result_and_no_external_work(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Helper attempted verifier access, subprocess, or a write")

    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "_git", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    assert evaluation.main(["historical"], root=ROOT) == 0
    assert "INVALID/2; missing canonical 09-glare; metrics=null; comparison=null; rules=not_evaluated" in capsys.readouterr().out


def test_release_without_selector_fails_closed(saved_project, capsys):
    assert not (saved_project / "release.json").exists()
    assert_invalid(saved_project, capsys)


def test_valid_selector_shape_and_exact_selection(release_project):
    assert evaluation.read_selector(release_project) == {
        "config": "gate.pass.yaml", "run_id": "offline-fixture-candidate"}


@pytest.mark.parametrize("decision,exit_code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
def test_actual_gate_outcomes_are_propagated(release_project, decision, exit_code, capsys):
    root = release_project
    run_id = "offline-fixture-candidate"
    if decision == "BLOCKED":
        mutate_result(root, run_id, "05-warning-title-case", status="pass")
    elif decision == "INVALID":
        (root / "results/candidate" / run_id / "09-glare.json").unlink()
    record_observation(root)
    assert evaluation.main(["release"], root=root) == exit_code
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == "" and result["decision"] == decision
    assert result["run_id"] == run_id and result["baseline_run_ids"] == list(evaluation.BASELINE_IDS)
    if decision != "INVALID":
        assert result["comparison_required"] is False and len(result["comparison"]["cases"]) == 12
        assert all(rule["status"] != "not_evaluated" for rule in result["rules"])


@pytest.mark.parametrize("selector", [
    {}, {"config": "gate.pass.yaml"}, {"run_id": "offline-fixture-candidate"},
    {"config": "gate.pass.yaml", "run_id": "offline-fixture-candidate", "extra": True},
    {"config": None, "run_id": "offline-fixture-candidate"},
    {"config": "gate.pass.yaml", "run_id": False},
    {"config": "gate.pass.yaml", "run_id": ""},
    {"config": "gate.pass.yaml", "run_id": " "},
    {"config": "/tmp/gate.pass.yaml", "run_id": "offline-fixture-candidate"},
    {"config": "../gate.pass.yaml", "run_id": "offline-fixture-candidate"},
    {"config": "configs/gate.pass.yaml", "run_id": "offline-fixture-candidate"},
    {"config": "configs\\gate.pass.yaml", "run_id": "offline-fixture-candidate"},
    {"config": "gate.yaml", "run_id": "offline-fixture-candidate"},
    {"config": "gate.experiment-01.yaml", "run_id": "offline-fixture-candidate"},
    {"config": "gate.pass.yaml", "run_id": "/tmp/run"},
    {"config": "gate.pass.yaml", "run_id": "../run"},
    {"config": "gate.pass.yaml", "run_id": "nested/run"},
    {"config": "gate.pass.yaml", "run_id": "nested\\run"},
    {"config": "gate.pass.yaml", "run_id": ".."},
    {"config": "gate.pass.yaml", "run_id": "run\0"},
    {"config": "gate.pass.yaml", "run_id": "candidate-smoke-01"},
])
def test_invalid_selector_values(release_project, selector, capsys):
    (release_project / "release.json").write_text(json.dumps(selector))
    assert_invalid(release_project, capsys)


@pytest.mark.parametrize("text", ['{', 'null', '[]',
    '{"config":"gate.pass.yaml","config":"gate.blocked.yaml","run_id":"offline-fixture-candidate"}',
    '{"config":"gate.pass.yaml","run_id":"offline-fixture-candidate","run_id":"other"}',
    '{"config":"gate.pass.yaml","run_id":NaN}',
])
def test_malformed_and_duplicate_json(release_project, text, capsys):
    (release_project / "release.json").write_text(text)
    assert_invalid(release_project, capsys)


@pytest.mark.parametrize("missing", ["gate.pass.yaml", "experiments.json", "release.json", "run"])
def test_missing_inputs(release_project, missing, capsys):
    if missing == "run":
        shutil.rmtree(release_project / "results/candidate/offline-fixture-candidate")
    else:
        (release_project / missing).unlink()
    assert_invalid(release_project, capsys)


@pytest.mark.parametrize("mutation", ["rules", "corpus", "candidate_pin", "baseline_pin", "baseline_incomplete"])
def test_release_revalidates_config_corpus_and_evidence(release_project, mutation, capsys):
    root = release_project
    if mutation == "rules":
        data = candidate_config(root)
        del data["rules"]["max_critical_dangerous_mistakes"]
        save_config(root, data, "gate.pass.yaml")
    elif mutation == "corpus":
        path = root / "cases/images/01-perfect.png"
        path.write_bytes(path.read_bytes() + b"changed")
    elif mutation in ("candidate_pin", "baseline_pin"):
        directory = root / ("results/candidate/offline-fixture-candidate" if mutation == "candidate_pin"
                            else "results/current/current-baseline-01")
        path = directory / "manifest.json"
        data = json.loads(path.read_text())
        data["fingerprint"]["prompt_sha256"] = "c" * 64
        path.write_text(json.dumps(data))
        if mutation == "candidate_pin":
            # Even re-recording the observation cannot make a mismatched pin PASS.
            record_observation(root)
    else:
        (root / "results/current/current-baseline-01/01-perfect.json").unlink()
    assert_invalid(root, capsys)


@pytest.mark.parametrize("text", ["null", "[]", '{"experiments":{}}',
    '{"experiments":[],"experiments":[]}', '{"experiments":[],"extra":true}',
])
def test_malformed_catalog_fails_closed(release_project, text, capsys):
    (release_project / "experiments.json").write_text(text)
    assert_invalid(release_project, capsys)


@pytest.mark.parametrize("name", ["release.json", "experiments.json", "gate.pass.yaml", "results",
                                  "results/candidate", "results/candidate/offline-fixture-candidate",
                                  "results/candidate/offline-fixture-candidate/manifest.json",
                                  "results/candidate/offline-fixture-candidate/01-perfect.json"])
def test_symlink_escape_at_each_input_level(release_project, tmp_path, name, capsys):
    path = release_project / name
    outside = tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    assert_invalid(release_project, capsys)


@pytest.mark.parametrize("mutation", ["unapproved", "exploratory", "duplicate", "wrong_pair",
                                      "extra", "missing", "wrong_hash", "bad_expected", "wrong_identity",
                                      "wrong_observed_outcome"])
def test_catalog_validation(release_project, mutation, capsys):
    path = release_project / "experiments.json"
    catalog = json.loads(path.read_text())
    entry = catalog["experiments"][0]
    if mutation == "unapproved":
        catalog["experiments"] = []
    elif mutation == "exploratory":
        entry["kind"] = "exploratory"
    elif mutation == "duplicate":
        catalog["experiments"].append(entry.copy())
    elif mutation == "wrong_pair":
        entry["config"] = "gate.yaml"
    elif mutation == "extra":
        entry["extra"] = "unused"
    elif mutation == "missing":
        del entry["evidence_sha256"]
    elif mutation == "wrong_hash":
        entry["evidence_sha256"] = "0" * 64
    elif mutation == "bad_expected":
        entry["expected"] = {"decision": "PASS"}
    elif mutation == "wrong_identity":
        entry["expected"]["run_id"] = "another-run"
    else:
        # Catalog labeling never overrides the real gate's BLOCKED decision.
        mutate_result(release_project, entry["run_id"], "05-warning-title-case", status="pass")
        entry["evidence_sha256"] = evaluation.evidence_digest(release_project, "candidate", entry["run_id"])
        path.write_text(json.dumps(catalog))
        assert evaluation.main(["release"], root=release_project) == 1
        assert json.loads(capsys.readouterr().out)["decision"] == "BLOCKED"
        return
    path.write_text(json.dumps(catalog))
    assert_invalid(release_project, capsys)


@pytest.mark.parametrize("mutation", ["comparison_required", "missing_comparison", "missing_rule",
                                      "unevaluated_rule", "wrong_threshold", "wrong_case_count"])
def test_nominal_pass_requires_complete_comparison_and_rules(release_project, monkeypatch, mutation, capsys):
    configs, cases = evaluation.validate_configs(release_project)
    original = evaluation.evaluate(release_project, configs["gate.pass.yaml"], cases, "offline-fixture-candidate")
    if mutation == "comparison_required":
        original = original.model_copy(update={"comparison_required": True})
    elif mutation == "missing_comparison":
        original = original.model_copy(update={"comparison": None})
    elif mutation == "missing_rule":
        original = original.model_copy(update={"rules": original.rules[:-1]})
    elif mutation == "unevaluated_rule":
        rule = original.rules[0].model_copy(update={"status": RuleStatus.NOT_EVALUATED, "actual": None})
        original = original.model_copy(update={"rules": [rule, *original.rules[1:]]})
    elif mutation == "wrong_threshold":
        rule = original.rules[0].model_copy(update={"threshold": 1})
        original = original.model_copy(update={"rules": [rule, *original.rules[1:]]})
    else:
        original = original.model_copy(update={"metrics": original.metrics.model_copy(update={"total_cases": 11})})
    monkeypatch.setattr(evaluation, "check_run", lambda **kwargs: original)
    assert_invalid(release_project, capsys)


@pytest.mark.parametrize("mutation", ["missing_another_case", "manifest_mismatch", "malformed_result", "valid_payload_change"])
def test_historical_rejects_changed_luna_cause_or_evidence(saved_project, mutation, capsys):
    directory = saved_project / "results/candidate" / evaluation.LUNA_RUN
    if mutation == "missing_another_case":
        (directory / "01-perfect.json").unlink()
    elif mutation == "manifest_mismatch":
        path = directory / "manifest.json"
        data = json.loads(path.read_text())
        data["fingerprint"]["declared_model"] = "other"
        path.write_text(json.dumps(data))
    elif mutation == "malformed_result":
        (directory / "01-perfect.json").write_text('{"secret":"DO_NOT_ECHO",')
    else:
        mutate_result(saved_project, evaluation.LUNA_RUN, "01-perfect", status="fail")
    assert evaluation.main(["historical"], root=saved_project) == 2
    output = capsys.readouterr()
    assert "INVALID/2" in output.err and "DO_NOT_ECHO" not in output.out + output.err


@pytest.mark.parametrize("unexpected", [Decision.PASS, Decision.BLOCKED])
def test_historical_rejects_unexpected_decision(saved_project, monkeypatch, unexpected, capsys):
    configs, cases = evaluation.validate_configs(saved_project)
    decision = evaluation.evaluate(saved_project, configs["gate.yaml"], cases, evaluation.LUNA_RUN)
    monkeypatch.setattr(evaluation, "evaluate", lambda *args: decision.model_copy(update={"decision": unexpected}))
    assert evaluation.main(["historical"], root=saved_project) == 2
    assert "Original Luna result" in capsys.readouterr().err


def test_historical_requires_baselines_despite_incomplete_luna(saved_project, capsys):
    (saved_project / "results/current/current-baseline-02/manifest.json").unlink()
    assert evaluation.main(["historical"], root=saved_project) == 2
    assert "INVALID/2" in capsys.readouterr().err


def test_catalog_historical_official_and_exploratory_outcomes(release_project, capsys):
    root = release_project
    record_observation(root, kind="exploratory")
    assert evaluation.main(["historical"], root=root) == 0
    assert "Historical experiment: PASS/0" in capsys.readouterr().out
    record_observation(root, kind="official")
    mutate_result(root, "offline-fixture-candidate", "05-warning-title-case", status="pass")
    path = root / "experiments.json"
    data = json.loads(path.read_text())
    data["experiments"][0]["evidence_sha256"] = evaluation.evidence_digest(root, "candidate", "offline-fixture-candidate")
    path.write_text(json.dumps(data))
    assert evaluation.main(["historical"], root=root) == 2
    assert "recorded observed outcome" in capsys.readouterr().err


def test_helper_is_read_only_on_all_saved_inputs(release_project, capsys):
    paths = [path for path in release_project.rglob("*") if path.is_file()]
    before = {path: (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) for path in paths}
    assert evaluation.main(["historical"], root=release_project) == 0
    assert evaluation.main(["release"], root=release_project) == 0
    capsys.readouterr()
    assert {path: (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) for path in paths} == before


@pytest.mark.parametrize("decision,code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
def test_helper_process_exit_contract(release_project, decision, code):
    if decision == "BLOCKED":
        mutate_result(release_project, "offline-fixture-candidate", "05-warning-title-case", status="pass")
    elif decision == "INVALID":
        (release_project / "results/candidate/offline-fixture-candidate/09-glare.json").unlink()
    record_observation(release_project)
    # Import the real helper in a child with an explicitly isolated fixture root.
    source = ("import importlib.util, pathlib; "
              "s=importlib.util.spec_from_file_location('evaluation',sys.argv[1]); "
              "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
              "raise SystemExit(m.main(['release'],root=pathlib.Path(sys.argv[2])))")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}
    result = subprocess.run([sys.executable, "-c", "import sys; " + source,
                             str(ROOT / "scripts/evaluate_saved.py"), str(release_project)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == code and result.stderr == ""
    assert json.loads(result.stdout)["decision"] == decision
