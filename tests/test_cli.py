import builtins
from pathlib import Path
import socket
import subprocess

import httpx
import pytest
import yaml

from ai_model_migration_gate.cli import build_parser, main
from ai_model_migration_gate.config import load_config
from ai_model_migration_gate.results import CaseResult, write_result
from ai_model_migration_gate import runner


REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def forbid_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("CLI attempted file, network, or verifier operations")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.mark.parametrize("command", [None, "run", "check", "report"])
def test_help(command, capsys, forbid_external_work):
    arguments = ["--help"] if command is None else [command, "--help"]
    with pytest.raises(SystemExit) as result:
        main(arguments)
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "usage: gate" in output
    if command is None:
        assert all(name in output for name in ("run", "check", "report"))
    else:
        assert f"usage: gate {command}" in output
        assert ("Plan a verifier run" if command == "run" else "Future:") in output


@pytest.mark.parametrize("command", ["check", "report"])
def test_placeholder(command, capsys, forbid_external_work):
    main([command])
    assert capsys.readouterr().out == "Not implemented yet.\n"


@pytest.fixture
def forbid_network_and_client_creation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("CLI attempted HTTP client construction, execution, or network access")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    monkeypatch.setattr(runner, "call_case", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def offline_project(tmp_path):
    (tmp_path / "gate.yaml").write_text(
        (REPOSITORY / "gate.yaml").read_text(encoding="utf-8"), encoding="utf-8",
    )
    (tmp_path / "cases/images").mkdir(parents=True)
    (tmp_path / "cases/cases.jsonl").write_text(
        (REPOSITORY / "cases/cases.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    for image in (REPOSITORY / "cases/images").glob("*.png"):
        (tmp_path / "cases/images" / image.name).write_bytes(image.read_bytes())
    return tmp_path


def test_run_parser_defaults():
    args = build_parser().parse_args(["run", "--target", "candidate", "--max-calls", "1"])
    assert args.target == "candidate"
    assert args.config == Path("gate.yaml")
    assert args.cases == Path("cases/cases.jsonl")
    assert args.yes is False
    assert args.force is False
    assert args.case_id is None
    assert args.run_id is None


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_run_parser_rejects_invalid_call_budget(value, capsys, forbid_external_work):
    with pytest.raises(SystemExit) as result:
        main(["run", "--target", "current", "--max-calls", value])
    assert result.value.code == 2
    assert "positive integer" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments, missing", [(["run", "--max-calls", "1"], "--target"),
                           (["run", "--target", "current"], "--max-calls")]
)
def test_run_parser_requires_target_and_budget(arguments, missing, capsys, forbid_external_work):
    with pytest.raises(SystemExit) as result:
        main(arguments)
    assert result.value.code == 2
    assert missing in capsys.readouterr().err


def test_run_parser_rejects_unknown_target(capsys, forbid_external_work):
    with pytest.raises(SystemExit) as result:
        main(["run", "--target", "other", "--max-calls", "1"])
    assert result.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_run_help_lists_all_safety_options(capsys, forbid_external_work):
    with pytest.raises(SystemExit) as result:
        main(["run", "--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    for option in ("--target", "--config", "--cases", "--yes", "--max-calls", "--case", "--force", "--run-id"):
        assert option in output


@pytest.mark.parametrize("target", ["current", "candidate"])
def test_unconfirmed_run_without_expected_pin_constructs_no_client_and_writes_no_results(
    offline_project, target, monkeypatch, capsys, forbid_network_and_client_creation,
):
    config_path = offline_project / "gate.yaml"
    config = load_config(config_path)
    getattr(config.targets, target).expected_fingerprint = None
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.chdir(offline_project)
    arguments = ["run", "--target", target, "--max-calls", "3", "--run-id", "planning-only"]

    def forbidden_write(*args, **kwargs):
        pytest.fail("Offline CLI planning attempted to write files")

    monkeypatch.setattr(Path, "mkdir", forbidden_write)
    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    main(arguments)
    output = capsys.readouterr().out
    assert "Selected calls: 3 / 3" in output
    assert "Run ID: planning-only" in output
    assert "real execution requires --yes" in output
    assert output.endswith("Plan only: live execution requires --yes.\n")
    assert not (offline_project / "results").exists()


def test_confirmed_run_requires_run_id_before_loading_or_client_creation(
    capsys, forbid_external_work, forbid_network_and_client_creation,
):
    with pytest.raises(SystemExit) as result:
        main(["run", "--target", "current", "--max-calls", "1", "--yes"])
    assert result.value.code == 2
    assert "--run-id is required with --yes" in capsys.readouterr().err


def test_case_and_image_paths_use_config_directory(
    offline_project, tmp_path, monkeypatch, capsys, forbid_network_and_client_creation,
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    main([
        "run", "--target", "candidate", "--config", str(offline_project / "gate.yaml"),
        "--cases", "cases/cases.jsonl", "--max-calls", "1", "--case", "09-glare",
    ])
    output = capsys.readouterr().out
    assert "target=candidate" in output
    assert "Cases: 09-glare" in output
    assert output.endswith("Plan only: live execution requires --yes.\n")
    assert not (offline_project / "results").exists()


def test_unknown_cli_case_fails_clearly(
    offline_project, monkeypatch, capsys, forbid_network_and_client_creation,
):
    monkeypatch.chdir(offline_project)
    with pytest.raises(SystemExit) as result:
        main(["run", "--target", "current", "--max-calls", "1", "--case", "unknown"])
    assert result.value.code == 2
    assert "Unknown case ID: unknown" in capsys.readouterr().err


@pytest.mark.parametrize("force", [False, True])
def test_cli_run_id_and_force_only_plan_and_preserve_existing_record(
    offline_project, force, monkeypatch, capsys, forbid_network_and_client_creation,
):
    result_file = write_result(offline_project / "results", "same-run", CaseResult(
        case_id="04-brand-case-only", target="current", status="success", response_time_ms=1,
        http_status=200, raw_response={},
    ))
    original = result_file.read_bytes()
    monkeypatch.chdir(offline_project)
    arguments = [
        "run", "--target", "current", "--max-calls", "1", "--case", "04-brand-case-only",
        "--run-id", "same-run",
    ]
    if force:
        arguments.append("--force")
    main(arguments)
    output = capsys.readouterr().out
    assert "Run ID: same-run" in output
    assert f"Selected calls: {int(force)} / 1" in output
    assert output.endswith("Plan only: live execution requires --yes.\n")
    assert result_file.read_bytes() == original
