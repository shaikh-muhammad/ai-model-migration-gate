"""Read-only CLI reports backed exclusively by temporary saved runs."""

import json
from pathlib import Path
import subprocess

import httpx
import pytest
import yaml

from ai_model_migration_gate import cli, fingerprint, runner, scoring
from ai_model_migration_gate.cli import build_parser, main
from ai_model_migration_gate.manifest import manifest_path
from ai_model_migration_gate.scoring import ScoredRun


@pytest.fixture(autouse=True)
def forbid_report_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Report attempted fingerprint recomputation, Git, verifier, or client work")

    monkeypatch.setattr(cli, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "_git", forbidden)
    monkeypatch.setattr(runner, "preflight_case", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)


def report_arguments(saved_run, json_output=False):
    args = ["report", "--target", "current", "--run-id", saved_run["run_id"],
            "--config", str(saved_run["results_dir"].parent / "gate.yaml")]
    return args + (["--json"] if json_output else [])


def test_report_parser_defaults():
    args = build_parser().parse_args(["report", "--target", "candidate", "--run-id", "run"])
    assert args.target == "candidate"
    assert args.run_id == "run"
    assert args.config == Path("gate.yaml")
    assert args.cases == Path("cases/cases.jsonl")
    assert args.json is False


@pytest.mark.parametrize("options, missing", [([], "--target"),
    (["--target", "current"], "--run-id"), (["--run-id", "run"], "--target")])
def test_report_requires_target_and_run_id(options, missing, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Incomplete report arguments caused file access")

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["report", *options])
    assert error.value.code != 0
    assert missing in capsys.readouterr().err


def test_report_help_reads_no_files(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Report help read a file")

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["report", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "Score one complete saved run offline" in output
    for option in ("--target", "--run-id", "--config", "--cases", "--json"):
        assert option in output


def test_complete_report_json_contains_only_structured_metrics_and_ordered_cases(saved_run, capsys):
    main(report_arguments(saved_run, json_output=True))
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert output.err == ""
    assert set(data) == {"target", "run_id", "metrics", "cases"}
    assert data["target"] == "current"
    assert data["run_id"] == saved_run["run_id"]
    assert data["metrics"] == {
        "total_cases": 12, "correct_cases": 12, "minor_cases": 0,
        "false_block_cases": 0, "dangerous_cases": 0, "critical_dangerous_cases": 0,
        "accuracy": 1.0, "median_response_time_ms": 650.0, "p95_response_time_ms": 1200.0,
    }
    assert [case["case_id"] for case in data["cases"]] == sorted(case.id for case in saved_run["cases"])
    assert len(ScoredRun.model_validate_json(output.out).cases) == 12
    assert all(set(case) == {"case_id", "expected_outcome", "actual_outcome", "classification",
                             "severity_rank", "critical", "tags", "response_time_ms"}
               for case in data["cases"])
    assert "raw_response" not in output.out
    assert "base_url" not in output.out


def test_complete_text_report_has_summary_and_canonical_case_order(saved_run, capsys):
    main(report_arguments(saved_run))
    output = capsys.readouterr().out
    assert f"Run report: target=current; run_id={saved_run['run_id']}" in output
    assert "total=12; correct=12; minor=0; false_blocks=0; dangerous=0; critical_dangerous=0" in output
    assert "Accuracy: 100.00%" in output
    assert "median=650.0; p95 (nearest-rank)=1200.0" in output
    case_lines = output.splitlines()[4:]
    assert len(case_lines) == 12
    for index, case in enumerate(sorted(saved_run["cases"], key=lambda case: case.id), start=1):
        assert case_lines[index - 1] == (
            f"{case.id}: expected={case.expected_outcome.value}; actual={case.expected_outcome.value}; "
            f"classification=correct; critical={'yes' if case.critical else 'no'}; response_time_ms={index * 100:.1f}"
        )
    assert "raw_response" not in output


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("invalid", ["missing_case", "corrupt_case", "raw_status", "missing_status",
    "null_status", "unexpected_json", "manifest", "manifest_identity", "fingerprint", "null_pin"])
def test_invalid_or_incomplete_cli_emits_no_partial_output_or_aggregate_metrics(
    saved_run, invalid, json_output, capsys, monkeypatch,
):
    root = manifest_path(saved_run["results_dir"], "current", saved_run["run_id"]).parent
    canonical = root / f"{saved_run['cases'][0].id}.json"
    if invalid == "missing_case":
        canonical.unlink()
    elif invalid == "corrupt_case":
        canonical.write_bytes(b"{")
    elif invalid in ("raw_status", "missing_status", "null_status"):
        data = json.loads(canonical.read_text())
        status = data["raw_response"]["verification"]["overall"]
        if invalid == "missing_status":
            status.pop("status")
        else:
            status["status"] = None if invalid == "null_status" else "PRIVATE_PAYLOAD_MARKER"
        data["raw_response"]["unrelated"] = "PRIVATE_PAYLOAD_MARKER"
        canonical.write_text(json.dumps(data), encoding="utf-8")
    elif invalid == "unexpected_json":
        (root / "unknown-case.json").write_bytes(b"unread")
    elif invalid == "manifest":
        (root / "manifest.json").unlink()
    elif invalid in ("manifest_identity", "fingerprint"):
        path = root / "manifest.json"
        data = json.loads(path.read_text())
        if invalid == "manifest_identity":
            data["run_id"] = "another-run"
        else:
            data["fingerprint"]["tool_version"] = "changed"
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        config_path = saved_run["results_dir"].parent / "gate.yaml"
        config = saved_run["config"].model_copy(deep=True)
        config.targets.current.expected_fingerprint = None
        config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid report reached aggregation")

    monkeypatch.setattr(scoring, "aggregate_scores", forbidden)
    with pytest.raises(SystemExit) as error:
        main(report_arguments(saved_run, json_output=json_output))
    assert error.value.code != 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "incomplete or invalid" in output.err
    assert "PRIVATE_PAYLOAD_MARKER" not in output.err
    assert "Accuracy:" not in output.err
    assert "median_response_time_ms" not in output.err
    assert "p95_response_time_ms" not in output.err
    if invalid == "fingerprint":
        assert "Fingerprint mismatch: tool_version" in output.err


@pytest.mark.parametrize("absolute_cases", [False, True])
def test_cases_and_results_resolve_from_selected_config_not_working_directory(
    saved_run, tmp_path, absolute_cases, monkeypatch, capsys,
):
    project = saved_run["results_dir"].parent
    cases_path = project / "custom/corpus.jsonl"
    cases_path.parent.mkdir()
    original_cases = project / "cases/cases.jsonl"
    cases_path.write_bytes(original_cases.read_bytes())
    original_cases.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "results").mkdir()
    monkeypatch.chdir(elsewhere)
    options = report_arguments(saved_run, json_output=True)
    options.extend(["--cases", str(cases_path) if absolute_cases else "custom/corpus.jsonl"])
    main(options)
    assert json.loads(capsys.readouterr().out)["metrics"]["total_cases"] == 12
    assert list((elsewhere / "results").iterdir()) == []


def test_report_orders_cases_by_id_even_when_corpus_lines_are_reversed(saved_run, capsys):
    path = saved_run["results_dir"].parent / "cases/cases.jsonl"
    path.write_text("\n".join(reversed(path.read_text().splitlines())) + "\n", encoding="utf-8")
    main(report_arguments(saved_run, json_output=True))
    cases = json.loads(capsys.readouterr().out)["cases"]
    assert [case["case_id"] for case in cases] == sorted(case.id for case in saved_run["cases"])


@pytest.mark.parametrize("json_output", [False, True])
def test_report_is_read_only_and_preserves_all_saved_bytes(saved_run, json_output, capsys, monkeypatch):
    project = saved_run["results_dir"].parent
    files = {path for path in project.rglob("*") if path.is_file()}
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    original_open = Path.open
    original_stat = Path.stat

    def guard_open(path, mode="r", *args, **kwargs):
        assert path in files
        assert not any(flag in mode for flag in ("w", "a", "x", "+"))
        return original_open(path, mode, *args, **kwargs)

    def guard_stat(path, *args, **kwargs):
        assert "absent-verifier" not in path.parts
        assert path.suffix != ".png"
        return original_stat(path, *args, **kwargs)

    def forbidden_write(*args, **kwargs):
        pytest.fail("Report attempted a filesystem write")

    monkeypatch.setattr(Path, "open", guard_open)
    monkeypatch.setattr(Path, "stat", guard_stat)
    monkeypatch.setattr(Path, "mkdir", forbidden_write)
    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    main(report_arguments(saved_run, json_output=json_output))
    assert capsys.readouterr().err == ""
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before


@pytest.mark.parametrize("run_id", ["current-smoke-20261007-001", "current-smoke-20261007-002"])
def test_temporary_pre_manifest_smoke_runs_are_rejected_without_reading_results(
    saved_run, run_id, capsys, monkeypatch,
):
    root = saved_run["results_dir"] / "current" / run_id
    root.mkdir(parents=True)
    path = root / "01-perfect.json"
    path.write_bytes(b"developmental evidence fixture\n")
    before = path.read_bytes(), path.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        pytest.fail("Pre-manifest run reached canonical scoring")

    monkeypatch.setattr(scoring, "read_result", forbidden)
    options = report_arguments({**saved_run, "run_id": run_id})
    with pytest.raises(SystemExit) as error:
        main(options)
    assert error.value.code != 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "Run manifest.json is required" in output.err
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(root.iterdir()) == [path]


def test_candidate_null_pin_report_is_rejected_without_verifier_access(saved_run, capsys):
    options = report_arguments(saved_run)
    options[options.index("current")] = "candidate"
    with pytest.raises(SystemExit) as error:
        main(options)
    assert error.value.code != 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "non-null expected_fingerprint" in output.err
