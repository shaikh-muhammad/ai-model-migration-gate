"""Offline execution fixtures; all Git mutations and results stay under tmp_path."""

from pathlib import Path
from datetime import datetime, timezone
import socket
import subprocess

import httpx
import pytest
import yaml

from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import load_config
from ai_model_migration_gate.fingerprint import PROMPT_RELATIVE_PATH, calculate_fingerprint
from ai_model_migration_gate.manifest import RunManifest, create_manifest
from ai_model_migration_gate.results import CaseResult, write_result


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline suite attempted real network access or HTTP transport construction")

    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    original_open = Path.open

    def guard_secrets(path, mode="r", *args, **kwargs):
        if path.name.startswith(".env") and ("r" in mode or "+" in mode):
            pytest.fail("Offline suite attempted to read an environment file")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_secrets)


@pytest.fixture
def temporary_git():
    def git(path, *arguments):
        return subprocess.run(
            ["git", "-c", "user.name=Offline Test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(path), *arguments],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    return git


@pytest.fixture
def offline_run_project(tmp_path, temporary_git):
    project = tmp_path / "project"
    project.mkdir()
    config_path = project / "gate.yaml"
    config_path.write_bytes((ROOT / "gate.yaml").read_bytes())
    (project / "cases/images").mkdir(parents=True)
    (project / "cases/cases.jsonl").write_bytes((ROOT / "cases/cases.jsonl").read_bytes())
    for image in (ROOT / "cases/images").glob("*.png"):
        (project / "cases/images" / image.name).write_bytes(image.read_bytes())

    verifier = tmp_path / "verifier"
    verifier.mkdir()
    temporary_git(verifier, "init", "--quiet")
    prompt = verifier / PROMPT_RELATIVE_PATH
    prompt.parent.mkdir()
    prompt.write_bytes(b'export const prompt = `Offline fixture`;\n')
    (verifier / ".gitignore").write_text(".env*\n", encoding="utf-8")
    temporary_git(verifier, "add", ".gitignore", str(PROMPT_RELATIVE_PATH))
    temporary_git(verifier, "commit", "--quiet", "-m", "Offline fixture")

    config = load_config(config_path)
    config.targets.current.verifier_path = Path("../verifier")
    config.targets.candidate.verifier_path = Path("../verifier")
    config.targets.current.expected_fingerprint = calculate_fingerprint(
        config, "current", config_path, load_cases(project / "cases/cases.jsonl"),
    )
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return project


@pytest.fixture
def saved_run(tmp_path):
    """Saved 12-case run with a committed pin and no verifier or image files."""
    project = tmp_path / "saved-project"
    project.mkdir()
    config_path = project / "gate.yaml"
    config = load_config(ROOT / "gate.yaml")
    for target in (config.targets.current, config.targets.candidate):
        target.verifier_path = Path("../absent-verifier")
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    cases_path = project / "cases/cases.jsonl"
    cases_path.parent.mkdir()
    cases_path.write_bytes((ROOT / "cases/cases.jsonl").read_bytes())
    cases = load_cases(cases_path)
    run_id = "saved-offline-run"
    results_dir = project / "results"
    manifest = RunManifest(
        run_id=run_id, target="current", created_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
        fingerprint=config.targets.current.expected_fingerprint,
    )
    create_manifest(results_dir, manifest)
    raw_statuses = {"Pass": "pass", "Needs Review": "needs_review", "Fail": "fail"}
    for index, case in enumerate(sorted(cases, key=lambda case: case.id), start=1):
        write_result(results_dir, run_id, CaseResult(
            case_id=case.id, target="current", status="success", http_status=200,
            response_time_ms=float(index * 100),
            raw_response={"verification": {"overall": {"status": raw_statuses[case.expected_outcome.value]}}},
        ))
    return {
        "config": config, "target": "current", "run_id": run_id,
        "cases": cases, "results_dir": results_dir,
    }


@pytest.fixture
def comparison_runs(saved_run):
    """Four complete saved runs; candidate configuration exists only in tmp_path."""
    config = saved_run["config"]
    config.targets.candidate.model = "offline-test-candidate"
    config.targets.candidate.expected_fingerprint = config.targets.current.expected_fingerprint.model_copy(
        update={"declared_model": "offline-test-candidate"},
    )
    baseline_ids = ["baseline-z", "baseline-a", "baseline-m"]
    run_id = "candidate-test"
    statuses = {"Pass": "pass", "Needs Review": "needs_review", "Fail": "fail"}
    for target, identity in [("current", value) for value in baseline_ids] + [("candidate", run_id)]:
        create_manifest(saved_run["results_dir"], RunManifest(
            run_id=identity, target=target, created_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
            fingerprint=getattr(config.targets, target).expected_fingerprint,
        ))
        for case in saved_run["cases"]:
            write_result(saved_run["results_dir"], identity, CaseResult(
                case_id=case.id, target=target, status="success", http_status=200,
                response_time_ms=100.0,
                raw_response={"verification": {"overall": {"status": statuses[case.expected_outcome.value]}}},
            ))
    (saved_run["results_dir"].parent / "gate.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8",
    )
    return {**saved_run, "target": "candidate", "run_id": run_id, "baseline_run_ids": baseline_ids}
