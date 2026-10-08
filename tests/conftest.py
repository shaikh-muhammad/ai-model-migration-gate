"""Offline execution fixtures; all Git mutations and results stay under tmp_path."""

from pathlib import Path
import socket
import subprocess

import httpx
import pytest
import yaml

from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import load_config
from ai_model_migration_gate.fingerprint import PROMPT_RELATIVE_PATH, calculate_fingerprint


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
