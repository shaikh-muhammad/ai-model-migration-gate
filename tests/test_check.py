"""CLI return codes and real process exits using temporary saved runs only."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import httpx
import pytest
import yaml

from ai_model_migration_gate import cli, fingerprint, runner, scoring
from ai_model_migration_gate.cli import build_parser, main
from ai_model_migration_gate.gate import GateDecision
from ai_model_migration_gate.manifest import manifest_path


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def forbid_check_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Check attempted fingerprint, Git, verifier, or HTTP client work")

    monkeypatch.setattr(cli, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "_git", forbidden)
    monkeypatch.setattr(runner, "preflight_case", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    original_popen = subprocess.Popen

    def guarded_popen(command, *args, **kwargs):
        assert isinstance(command, list) and command[0] == sys.executable
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


def save_policy(run):
    path = run["results_dir"].parent / "gate.yaml"
    path.write_text(yaml.safe_dump(run["config"].model_dump(mode="json")), encoding="utf-8")


@pytest.fixture
def frozen_run(saved_run):
    saved_run["config"].rules.min_correct_cases = 11
    save_policy(saved_run)
    return saved_run


def arguments(run, json_output=True):
    args = ["check", "--target", "current", "--run-id", run["run_id"],
            "--config", str(run["results_dir"].parent / "gate.yaml")]
    return args + (["--json"] if json_output else [])


def make_blocked(run):
    root = manifest_path(run["results_dir"], "current", run["run_id"]).parent
    path = root / f"{run['cases'][-1].id}.json"
    data = json.loads(path.read_text())
    data["response_time_ms"] = 5000.001
    path.write_text(json.dumps(data), encoding="utf-8")


def test_check_parser_defaults():
    parsed = build_parser().parse_args(["check", "--target", "candidate", "--run-id", "run"])
    assert parsed.target == "candidate" and parsed.run_id == "run"
    assert parsed.config == Path("gate.yaml") and parsed.cases == Path("cases/cases.jsonl")
    assert parsed.json is False


@pytest.mark.parametrize("options, missing", [([], "--target"),
    (["--target", "current"], "--run-id"), (["--run-id", "run"], "--target")])
def test_missing_required_check_arguments_use_argparse_system_exit(options, missing, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid CLI arguments caused file access")

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["check", *options])
    assert error.value.code == 2
    assert missing in capsys.readouterr().err


def test_check_help_does_not_load_files(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Check help read files")

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["check", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    help_text = " ".join(output.split())
    assert "Check absolute rules" in help_text and "comparison is still required" in help_text
    assert all(option in output for option in ("--target", "--run-id", "--config", "--cases", "--json"))


@pytest.mark.parametrize("decision, code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
def test_main_returns_exact_codes_and_json_without_runtime_argparse_errors(
    frozen_run, decision, code, capsys, monkeypatch,
):
    if decision == "BLOCKED":
        make_blocked(frozen_run)
    elif decision == "INVALID":
        frozen_run["config"].rules.min_correct_cases = None
        save_policy(frozen_run)

    def forbidden_error(*args, **kwargs):
        pytest.fail("Runtime gate decisions must not use argparse.error")

    monkeypatch.setattr(argparse.ArgumentParser, "error", forbidden_error)
    assert main(arguments(frozen_run)) == code
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert data["decision"] == decision
    assert GateDecision.model_validate_json(output.out).exit_code == code
    assert data["comparison_required"] is True
    assert data["rules"][-1]["status"] == "not_evaluated"
    assert output.err == ""
    assert data["metrics"]["total_cases"] == 12
    assert "raw_response" not in output.out


@pytest.mark.parametrize("decision, code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
def test_normal_decision_output_is_concise_and_marks_comparison_required(frozen_run, decision, code, capsys):
    if decision == "BLOCKED":
        make_blocked(frozen_run)
    elif decision == "INVALID":
        frozen_run["config"].rules.min_correct_cases = None
        save_policy(frozen_run)
    assert main(arguments(frozen_run, json_output=False)) == code
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith(f"{decision}: target=current; run_id={frozen_run['run_id']}")
    assert "Correct cases: 12/12; critical dangerous cases: 0" in output.out
    assert "p95 client response time (ms):" in output.out
    assert "max_dangerous_regressions: not_evaluated" in output.out
    assert "Comparison required:" in output.out
    assert "absolute single-run check" in output.out
    assert len(output.out.splitlines()) <= 8
    if decision == "INVALID":
        assert "min_correct_cases is null" in output.out


@pytest.mark.parametrize("invalid", ["missing_case", "missing_manifest", "fingerprint", "corrupt_case",
    "unknown_status", "missing_status", "unexpected_json"])
@pytest.mark.parametrize("json_output", [False, True])
def test_structural_invalidity_renders_decision_and_returns_two_without_partial_metrics(
    frozen_run, invalid, json_output, capsys, monkeypatch,
):
    root = manifest_path(frozen_run["results_dir"], "current", frozen_run["run_id"]).parent
    canonical = root / f"{frozen_run['cases'][0].id}.json"
    if invalid == "missing_case":
        canonical.unlink()
    elif invalid == "missing_manifest":
        (root / "manifest.json").unlink()
    elif invalid == "fingerprint":
        path = root / "manifest.json"
        data = json.loads(path.read_text())
        data["fingerprint"]["prompt_sha256"] = "changed"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif invalid == "corrupt_case":
        canonical.write_bytes(b"{")
    elif invalid in ("unknown_status", "missing_status"):
        data = json.loads(canonical.read_text())
        overall = data["raw_response"]["verification"]["overall"]
        if invalid == "missing_status":
            overall.pop("status")
        else:
            overall["status"] = "PRIVATE_PAYLOAD_MARKER"
        canonical.write_text(json.dumps(data), encoding="utf-8")
    else:
        (root / "unexpected.json").write_bytes(b"unread evidence")

    def forbidden_error(*args, **kwargs):
        pytest.fail("Runtime INVALID used argparse.error or attempted aggregation")

    monkeypatch.setattr(argparse.ArgumentParser, "error", forbidden_error)
    monkeypatch.setattr(scoring, "aggregate_scores", forbidden_error)
    assert main(arguments(frozen_run, json_output=json_output)) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert "PRIVATE_PAYLOAD_MARKER" not in output.out
    if json_output:
        data = json.loads(output.out)
        assert data["decision"] == "INVALID"
        assert data["metrics"] is None
        assert data["scoring_problems"] is not None
        assert all(rule["actual"] is None for rule in data["rules"])
        assert all(rule["status"] == "not_evaluated" for rule in data["rules"])
        if invalid == "fingerprint":
            assert "Fingerprint mismatch: prompt_sha256" in data["errors"][0]
    else:
        assert output.out.startswith("INVALID:") and "Cause:" in output.out
        assert "Correct cases:" not in output.out


@pytest.mark.parametrize("invalid", ["config_missing", "config_yaml", "config_policy", "cases_missing", "cases_json"])
def test_runtime_input_errors_produce_invalid_json_not_argparse_errors(frozen_run, invalid, capsys, monkeypatch):
    project = frozen_run["results_dir"].parent
    config_path, cases_path = project / "gate.yaml", project / "cases/cases.jsonl"
    if invalid == "config_missing":
        config_path.unlink()
    elif invalid == "config_yaml":
        config_path.write_bytes(b"{")
    elif invalid == "config_policy":
        data = frozen_run["config"].model_dump(mode="json")
        data["rules"]["min_correct_cases"] = -1
        config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    elif invalid == "cases_missing":
        cases_path.unlink()
    else:
        cases_path.write_bytes(b"PRIVATE_INPUT_PAYLOAD_MARKER")

    def forbidden_error(*args, **kwargs):
        pytest.fail("Runtime input error used argparse.error")

    monkeypatch.setattr(argparse.ArgumentParser, "error", forbidden_error)
    assert main(arguments(frozen_run)) == 2
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert output.err == ""
    assert data["decision"] == "INVALID" and data["metrics"] is None
    expected_input = "selected configuration" if invalid.startswith("config") else "selected case corpus"
    assert expected_input in data["errors"][0]
    assert "PRIVATE_INPUT_PAYLOAD_MARKER" not in output.out


@pytest.mark.parametrize("absolute_cases", [False, True])
def test_check_resolves_cases_and_results_from_config_directory(frozen_run, tmp_path, absolute_cases, monkeypatch, capsys):
    project = frozen_run["results_dir"].parent
    custom_cases = project / "custom/corpus.jsonl"
    custom_cases.parent.mkdir()
    default_cases = project / "cases/cases.jsonl"
    custom_cases.write_bytes(default_cases.read_bytes())
    default_cases.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "results").mkdir()
    monkeypatch.chdir(elsewhere)
    options = arguments(frozen_run) + ["--cases", str(custom_cases) if absolute_cases else "custom/corpus.jsonl"]
    assert main(options) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "PASS"
    assert list((elsewhere / "results").iterdir()) == []


def test_check_does_not_change_report_output(frozen_run, capsys):
    options = arguments(frozen_run)
    options[0] = "report"
    assert main(options) is None
    before = capsys.readouterr().out
    assert main(arguments(frozen_run)) == 0
    capsys.readouterr()
    assert main(options) is None
    assert capsys.readouterr().out == before


@pytest.mark.parametrize("run_id", ["current-smoke-20261007-001", "current-smoke-20261007-002"])
def test_temporary_pre_manifest_smoke_runs_are_invalid_and_untouched(frozen_run, run_id, capsys, monkeypatch):
    root = frozen_run["results_dir"] / "current" / run_id
    root.mkdir(parents=True)
    path = root / "01-perfect.json"
    path.write_bytes(b"developmental fixture only\n")
    before = path.read_bytes(), path.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        pytest.fail("Pre-manifest check read canonical results")

    monkeypatch.setattr(scoring, "read_result", forbidden)
    assert main(arguments({**frozen_run, "run_id": run_id})) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["decision"] == "INVALID" and data["metrics"] is None
    assert "manifest.json is required" in data["errors"][0]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(root.iterdir()) == [path]


def test_candidate_null_pin_is_invalid_without_verifier_access(frozen_run, capsys):
    options = arguments(frozen_run)
    options[options.index("current")] = "candidate"
    assert main(options) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["metrics"] is None
    assert "non-null expected_fingerprint" in data["errors"][0]


def test_check_cli_reads_only_saved_inputs_and_writes_nothing(frozen_run, capsys, monkeypatch):
    project = frozen_run["results_dir"].parent
    files = {path for path in project.rglob("*") if path.is_file()}
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
        pytest.fail("Check CLI attempted a write")

    monkeypatch.setattr(Path, "open", guard_open)
    monkeypatch.setattr(Path, "stat", guard_stat)
    monkeypatch.setattr(Path, "mkdir", forbidden_write)
    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    assert main(arguments(frozen_run)) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "PASS"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before


@pytest.fixture
def subprocess_environment(tmp_path):
    guards = tmp_path / "guards"
    guards.mkdir()
    (guards / "sitecustomize.py").write_text(textwrap.dedent('''
        import socket
        import subprocess
        import httpx
        from ai_model_migration_gate import fingerprint, runner

        def forbidden(*args, **kwargs):
            raise RuntimeError("Offline subprocess attempted external work")

        socket.socket.connect = forbidden
        socket.socket.connect_ex = forbidden
        socket.create_connection = forbidden
        socket.getaddrinfo = forbidden
        httpx.Client = forbidden
        httpx.HTTPTransport = forbidden
        fingerprint.calculate_fingerprint = forbidden
        fingerprint._git = forbidden
        runner.preflight_case = forbidden
        subprocess.Popen = forbidden
    '''), encoding="utf-8")
    return {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join([str(guards), str(ROOT / "src")])}


@pytest.mark.parametrize("decision, code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
@pytest.mark.parametrize("entrypoint", ["module", "console_function"])
def test_actual_subprocess_exit_contract(frozen_run, subprocess_environment, decision, code, entrypoint):
    if decision == "BLOCKED":
        make_blocked(frozen_run)
    elif decision == "INVALID":
        frozen_run["config"].rules.min_correct_cases = None
        save_policy(frozen_run)
    if entrypoint == "module":
        command = [sys.executable, "-m", "ai_model_migration_gate.cli"]
    else:
        command = [sys.executable, "-c", "from ai_model_migration_gate.cli import main; raise SystemExit(main())"]
    completed = subprocess.run(
        command + arguments(frozen_run), cwd=frozen_run["results_dir"].parent,
        env=subprocess_environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == code
    assert completed.stderr == ""
    data = json.loads(completed.stdout)
    assert data["decision"] == decision
    assert data["comparison_required"] is True


def test_actual_subprocess_structural_invalid_is_json_with_exit_two(frozen_run, subprocess_environment):
    manifest_path(frozen_run["results_dir"], "current", frozen_run["run_id"]).unlink()
    completed = subprocess.run(
        [sys.executable, "-m", "ai_model_migration_gate.cli", *arguments(frozen_run)],
        env=subprocess_environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2 and completed.stderr == ""
    data = json.loads(completed.stdout)
    assert data["decision"] == "INVALID" and data["metrics"] is None


def test_actual_subprocess_cli_usage_error_retains_argparse_behavior(subprocess_environment):
    completed = subprocess.run(
        [sys.executable, "-m", "ai_model_migration_gate.cli", "check", "--json"],
        env=subprocess_environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "required" in completed.stderr and "usage:" in completed.stderr
