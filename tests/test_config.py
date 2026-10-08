from pathlib import Path
import socket
import subprocess

import pytest
from pydantic import ValidationError

from ai_model_migration_gate.config import FingerprintIdentity, GateRules, Targets, load_config


EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "gate.yaml"


def test_example_config_validates():
    config = load_config(EXAMPLE_CONFIG)
    assert set(Targets.model_fields) == {"current", "candidate"}
    assert config.targets.current.model == "gpt-5.4-mini"
    assert config.targets.candidate.model == "gpt-6-luna"
    assert config.targets.current.expected_fingerprint == FingerprintIdentity(
        declared_model="gpt-5.4-mini",
        prompt_sha256="af9e1e7e4c3efbe6c25d4a07cab7f8c5dfcaf0798f396134893fae5940d6d996",
        verifier_commit_sha="92bef14aa05260a2c7f39aa104a5a202aea099ce",
        case_set_sha256="fd1aa6b77276f122930f6d3ad85e05d992c2a67342d0ca6610cc8a8642291245",
        tool_version="0.1.0",
    )
    assert config.targets.candidate.expected_fingerprint == config.targets.current.expected_fingerprint.model_copy(
        update={"declared_model": "gpt-6-luna"},
    )
    assert config.targets.current.verifier_path == Path("../verifier-current")
    assert config.targets.candidate.verifier_path == Path("../verifier-candidate")
    assert config.rules == GateRules(min_correct_cases=10)


@pytest.mark.parametrize("name", ["current", "candidate"])
def test_missing_target_is_rejected(name):
    targets = load_config(EXAMPLE_CONFIG).targets.model_dump()
    del targets[name]
    with pytest.raises(ValidationError, match=name):
        Targets.model_validate(targets)


def test_extra_target_is_rejected():
    targets = load_config(EXAMPLE_CONFIG).targets.model_dump()
    targets["other"] = targets["current"]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Targets.model_validate(targets)


def test_fingerprint_identity_has_exact_fields():
    assert set(FingerprintIdentity.model_fields) == {
        "declared_model", "prompt_sha256", "verifier_commit_sha",
        "case_set_sha256", "tool_version",
    }


def test_config_loading_opens_only_supplied_file(monkeypatch):
    original_open = Path.open
    opened = []

    def checked_open(path, *args, **kwargs):
        assert path == EXAMPLE_CONFIG
        opened.append(path)
        return original_open(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Config loading attempted network or verifier access")

    monkeypatch.setattr(Path, "open", checked_open)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    load_config(EXAMPLE_CONFIG)
    assert opened == [EXAMPLE_CONFIG]
