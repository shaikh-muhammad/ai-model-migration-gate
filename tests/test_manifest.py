from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_model_migration_gate import fingerprint
from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import FingerprintIdentity, load_config
from ai_model_migration_gate.manifest import (
    RunManifest, create_manifest, manifest_path, prepare_run_manifest,
    require_fingerprint_match,
)
from ai_model_migration_gate.results import result_path


NOW = datetime(2026, 10, 7, 12, 34, 56, tzinfo=timezone.utc)


@pytest.fixture
def arguments(offline_run_project):
    project = offline_run_project
    return {
        "config": load_config(project / "gate.yaml"), "target": "current",
        "run_id": "offline-run", "config_path": project / "gate.yaml",
        "cases": load_cases(project / "cases/cases.jsonl"), "results_dir": project / "results",
    }


@pytest.fixture
def manifest_data(arguments):
    return {
        "run_id": arguments["run_id"], "target": "current", "created_at": NOW,
        "fingerprint": arguments["config"].targets.current.expected_fingerprint,
    }


def test_manifest_accepts_exactly_four_required_fields(manifest_data):
    manifest = RunManifest.model_validate(manifest_data)
    assert set(RunManifest.model_fields) == {"run_id", "target", "created_at", "fingerprint"}
    assert set(manifest.model_dump()) == set(manifest_data)
    assert manifest.created_at == NOW
    assert manifest.fingerprint == manifest_data["fingerprint"]
    assert RunManifest.model_validate_json(manifest.model_dump_json()) == manifest


@pytest.mark.parametrize("field", ["run_id", "target", "created_at", "fingerprint"])
def test_every_manifest_field_is_required(manifest_data, field):
    del manifest_data[field]
    with pytest.raises(ValidationError, match="Field required"):
        RunManifest.model_validate(manifest_data)


@pytest.mark.parametrize("field", ["hostname", "username", "cost", "environment", "api_key_state"])
def test_extra_metadata_rejected(manifest_data, field):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RunManifest.model_validate({**manifest_data, field: "unused"})


@pytest.mark.parametrize("value", [NOW.replace(tzinfo=None), "2026-10-07T12:34:56"])
def test_naive_timestamp_rejected(manifest_data, value):
    with pytest.raises(ValidationError, match="timezone_aware"):
        if isinstance(value, str):
            data = {**manifest_data, "created_at": value,
                    "fingerprint": manifest_data["fingerprint"].model_dump()}
            RunManifest.model_validate_json(json.dumps(data))
        else:
            RunManifest.model_validate({**manifest_data, "created_at": value})


@pytest.mark.parametrize("field, value", [("run_id", 123), ("target", "other"),
                                         ("created_at", 123), ("fingerprint", "invalid")])
def test_manifest_rejects_wrong_field_types(manifest_data, field, value):
    with pytest.raises(ValidationError):
        RunManifest.model_validate({**manifest_data, field: value})


def test_aware_timestamp_normalized_to_utc(manifest_data):
    local = NOW.astimezone(timezone(timedelta(hours=-4)))
    manifest = RunManifest.model_validate({**manifest_data, "created_at": local})
    assert manifest.created_at == NOW
    assert manifest.created_at.tzinfo == timezone.utc
    assert json.loads(manifest.model_dump_json())["created_at"] == "2026-10-07T12:34:56Z"


@pytest.mark.parametrize("target", ["current", "candidate"])
def test_manifest_layout(tmp_path, target):
    assert manifest_path(tmp_path / "results", target, "run") == (
        tmp_path / "results" / target / "run" / "manifest.json"
    )


@pytest.mark.parametrize("run_id", ["", ".", "..", "../escape", "/absolute", "a/b", "a\\b", "bad\0id"])
def test_paths_share_unsafe_run_id_rules(tmp_path, run_id):
    for path_function, arguments in (
        (manifest_path, (tmp_path, "current", run_id)),
        (result_path, (tmp_path, "current", run_id, "case")),
    ):
        with pytest.raises(ValueError, match="run_id.*filename component"):
            path_function(*arguments)


def test_invalid_target_rejected(tmp_path):
    with pytest.raises(ValueError, match="target must be"):
        manifest_path(tmp_path, "other", "run")


def test_result_cannot_overwrite_manifest_path(tmp_path):
    with pytest.raises(ValueError, match="reserved"):
        result_path(tmp_path, "current", "run", "manifest")


def test_new_manifest_has_exact_computed_and_expected_identity(arguments):
    computed = fingerprint.calculate_fingerprint(
        arguments["config"], "current", arguments["config_path"], arguments["cases"],
    )
    manifest = prepare_run_manifest(**arguments, created_at=NOW)
    path = manifest_path(arguments["results_dir"], "current", arguments["run_id"])
    assert manifest.fingerprint == computed == arguments["config"].targets.current.expected_fingerprint
    assert manifest.created_at == NOW
    assert path.read_bytes().endswith(b"\n")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {"run_id": "offline-run", "target": "current",
                      "created_at": "2026-10-07T12:34:56Z", "fingerprint": computed.model_dump()}


def test_default_timestamp_is_aware_utc(arguments):
    before = datetime.now(timezone.utc)
    manifest = prepare_run_manifest(**arguments)
    assert before <= manifest.created_at <= datetime.now(timezone.utc)
    assert manifest.created_at.tzinfo == timezone.utc


def test_timestamp_is_not_fingerprint_identity(manifest_data):
    first = RunManifest.model_validate(manifest_data)
    second = RunManifest.model_validate({**manifest_data, "created_at": NOW + timedelta(days=1)})
    assert first.created_at != second.created_at
    assert first.fingerprint == second.fingerprint
    assert "created_at" not in FingerprintIdentity.model_fields
    with pytest.raises(ValidationError, match="extra_forbidden"):
        FingerprintIdentity.model_validate({**first.fingerprint.model_dump(), "created_at": NOW})


def test_manifest_creation_is_exclusive(arguments, manifest_data):
    first = RunManifest.model_validate(manifest_data)
    path = create_manifest(arguments["results_dir"], first)
    before = path.read_bytes(), path.stat().st_mtime_ns
    replacement = RunManifest.model_validate({**manifest_data, "created_at": NOW + timedelta(days=1)})
    with pytest.raises(FileExistsError):
        create_manifest(arguments["results_dir"], replacement)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_resume_preserves_bytes_and_never_opens_for_writing(arguments, monkeypatch):
    first = prepare_run_manifest(**arguments, created_at=NOW)
    path = manifest_path(arguments["results_dir"], "current", "offline-run")
    # Valid but differently formatted JSON proves resume does not reserialize.
    path.write_text(json.dumps(first.model_dump(mode="json")) + "\n\n", encoding="utf-8")
    before = path.read_bytes(), path.stat().st_mtime_ns
    original_open = Path.open

    def forbid_writes(path, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in ("w", "a", "x", "+"))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_writes)
    assert prepare_run_manifest(**arguments, created_at=NOW + timedelta(days=1)) == first
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("field, value", [("run_id", "different"), ("target", "candidate")])
def test_manifest_identity_must_match_path(arguments, field, value, monkeypatch):
    original = prepare_run_manifest(**arguments, created_at=NOW)
    path = manifest_path(arguments["results_dir"], "current", "offline-run")
    data = original.model_dump(mode="json")
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Path mismatch should fail before fingerprint calculation")

    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    with pytest.raises(ValueError, match=f"Manifest {field} does not match"):
        prepare_run_manifest(**arguments)
    assert path.read_bytes() == before


@pytest.mark.parametrize("data", ["{", '{}', '{"hostname":"unused"}'])
def test_invalid_existing_manifest_is_not_replaced(arguments, data):
    path = manifest_path(arguments["results_dir"], "current", "offline-run")
    path.parent.mkdir(parents=True)
    path.write_text(data, encoding="utf-8")
    with pytest.raises(ValidationError):
        prepare_run_manifest(**arguments)
    assert path.read_text(encoding="utf-8") == data


def test_null_pin_fails_before_fingerprint_calculation(arguments, monkeypatch):
    arguments["config"].targets.current.expected_fingerprint = None

    def forbidden(*args, **kwargs):
        pytest.fail("Null expected identity must fail before fingerprint calculation")

    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    with pytest.raises(ValueError, match="non-null expected_fingerprint"):
        prepare_run_manifest(**arguments)
    assert not arguments["results_dir"].exists()


def test_manifest_must_match_expected_before_current_is_calculated(arguments, monkeypatch):
    prepare_run_manifest(**arguments)
    pin = arguments["config"].targets.current.expected_fingerprint
    arguments["config"].targets.current.expected_fingerprint = pin.model_copy(
        update={"declared_model": "changed", "tool_version": "changed"},
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Expected/manifest mismatch should fail before current calculation")

    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    with pytest.raises(ValueError) as error:
        prepare_run_manifest(**arguments)
    assert str(error.value) == "Fingerprint mismatch: declared_model, tool_version"


@pytest.mark.parametrize("fields", [["declared_model"], ["prompt_sha256", "verifier_commit_sha"],
                                    list(FingerprintIdentity.model_fields)])
def test_mismatch_reports_only_changed_field_names(manifest_data, fields):
    expected = manifest_data["fingerprint"]
    actual = expected.model_copy(update={field: "changed" for field in fields})
    with pytest.raises(ValueError) as error:
        require_fingerprint_match(actual, expected)
    assert str(error.value) == "Fingerprint mismatch: " + ", ".join(fields)


@pytest.mark.parametrize("run_id", ["old-run", "current-smoke-20261007-001", "current-smoke-20261007-002"])
def test_pre_manifest_directories_rejected_using_only_temporary_fixtures(arguments, run_id):
    arguments["run_id"] = run_id
    path = manifest_path(arguments["results_dir"], "current", run_id)
    path.parent.mkdir(parents=True)
    result = path.parent / "01-perfect.json"
    result.write_bytes(b"developmental fixture\n")
    before = result.read_bytes()
    with pytest.raises(ValueError, match="Pre-manifest or invalid run.*new run ID"):
        prepare_run_manifest(**arguments)
    assert result.read_bytes() == before
    assert not path.exists()


def test_empty_existing_run_directory_can_be_initialized(arguments):
    path = manifest_path(arguments["results_dir"], "current", "offline-run")
    path.parent.mkdir(parents=True)
    assert prepare_run_manifest(**arguments).fingerprint == arguments["config"].targets.current.expected_fingerprint
    assert path.is_file()


def test_ignored_synthetic_environment_file_does_not_change_manifest_identity(arguments):
    first = prepare_run_manifest(**arguments)
    verifier = arguments["config_path"].parent.parent / "verifier"
    (verifier / ".env.local").write_text("OFFLINE_FIXTURE_ONLY=unused\n", encoding="utf-8")
    assert prepare_run_manifest(**arguments) == first
