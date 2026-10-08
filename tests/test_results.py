import json

import pytest
from pydantic import ValidationError

from ai_model_migration_gate.results import (
    CaseResult, ResultStatus, attempt_path, read_result, result_path, write_attempt, write_result,
)


@pytest.mark.parametrize("status", [ResultStatus.SUCCESS, ResultStatus.ERROR])
def test_result_round_trip_and_exact_layout(tmp_path, status):
    result = CaseResult(
        case_id="01-perfect", target="current", status=status,
        http_status=200 if status == ResultStatus.SUCCESS else 503,
        response_time_ms=125.5, raw_response={"processingTimeMs": 8},
        error_code=None if status == ResultStatus.SUCCESS else "UNAVAILABLE",
    )
    path = write_attempt(tmp_path / "results", "offline-run", 1, result)
    assert path == tmp_path / "results/current/offline-run/attempts/01-perfect/0001.json"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == status.value
    if status == ResultStatus.SUCCESS:
        canonical = write_result(tmp_path / "results", "offline-run", result)
        assert canonical == tmp_path / "results/current/offline-run/01-perfect.json"
        assert canonical.read_bytes() == path.read_bytes()
        assert read_result(tmp_path / "results", "current", "offline-run", "01-perfect") == result
    else:
        assert read_result(tmp_path / "results", "current", "offline-run", "01-perfect") is None


def test_missing_result_does_not_create_directories(tmp_path):
    root = tmp_path / "results"
    assert read_result(root, "candidate", "new-run", "01-perfect") is None
    assert not root.exists()


def test_transport_error_round_trip_without_http_status(tmp_path):
    result = CaseResult(
        case_id="01-perfect", target="candidate", status="error",
        response_time_ms=1, error_message="Synthetic connection failure",
    )
    path = write_attempt(tmp_path, "offline-run", 1, result)
    assert path == attempt_path(tmp_path, "candidate", "offline-run", "01-perfect", 1)
    loaded = CaseResult.model_validate_json(path.read_text())
    assert loaded == result
    assert loaded.http_status is None
    assert loaded.raw_response is None
    assert read_result(tmp_path, "candidate", "offline-run", "01-perfect") is None


@pytest.mark.parametrize(
    "run_id, case_id", [("../escape", "01-perfect"), ("run", "../escape"), ("", "case"), ("run", "a\\b")]
)
def test_result_path_rejects_unsafe_components(tmp_path, run_id, case_id):
    with pytest.raises(ValueError, match="filename component"):
        result_path(tmp_path, "current", run_id, case_id)


def test_record_identity_must_match_the_path(tmp_path):
    path = result_path(tmp_path, "current", "run", "01-perfect")
    path.parent.mkdir(parents=True)
    path.write_text(CaseResult(
        case_id="other-case", target="candidate", status="success", response_time_ms=1,
        http_status=200, raw_response={},
    ).model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="identity does not match"):
        read_result(tmp_path, "current", "run", "01-perfect")


def test_corrupt_result_is_not_silently_treated_as_missing(tmp_path):
    path = result_path(tmp_path, "current", "run", "01-perfect")
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        read_result(tmp_path, "current", "run", "01-perfect")


@pytest.fixture
def success_data():
    return {
        "case_id": "01-perfect", "target": "current", "status": "success",
        "http_status": 200, "response_time_ms": 1,
        "raw_response": {"processingTimeMs": 8},
    }


@pytest.mark.parametrize("http_status", [200, 299])
def test_successful_record_is_valid(success_data, http_status):
    success_data["http_status"] = http_status
    result = CaseResult.model_validate(success_data)
    assert result.status == ResultStatus.SUCCESS
    assert result.http_status == http_status
    assert result.raw_response == {"processingTimeMs": 8}


def test_success_without_http_status_is_rejected(success_data):
    del success_data["http_status"]
    with pytest.raises(ValidationError, match="http_status in the HTTP 2xx range"):
        CaseResult.model_validate(success_data)


@pytest.mark.parametrize("http_status", [199, 300, 503])
def test_success_with_non_2xx_status_is_rejected(success_data, http_status):
    success_data["http_status"] = http_status
    with pytest.raises(ValidationError, match="http_status in the HTTP 2xx range"):
        CaseResult.model_validate(success_data)


def test_success_without_raw_response_is_rejected(success_data):
    del success_data["raw_response"]
    with pytest.raises(ValidationError, match="require raw_response"):
        CaseResult.model_validate(success_data)


def test_success_allows_present_empty_response_without_schema_validation(success_data):
    success_data["raw_response"] = {}
    assert CaseResult.model_validate(success_data).raw_response == {}


@pytest.mark.parametrize(
    "field, value", [("http_status", None), ("http_status", 503), ("raw_response", None)]
)
def test_read_result_rejects_malformed_success(tmp_path, success_data, field, value):
    success_data[field] = value
    path = result_path(tmp_path, "current", "same-run", "01-perfect")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(success_data), encoding="utf-8")
    with pytest.raises(ValidationError, match="Successful results require"):
        read_result(tmp_path, "current", "same-run", "01-perfect")
