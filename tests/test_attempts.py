"""Storage invariants using only temporary result directories."""

import json

import pytest
from pydantic import ValidationError

from ai_model_migration_gate.results import (
    CaseResult, ResultStatus, attempt_directory, attempt_path, next_attempt_number,
    read_result, result_path, write_attempt, write_result,
)


@pytest.fixture
def result():
    return CaseResult(
        case_id="case-a", target="current", status="success", http_status=200,
        response_time_ms=3, raw_response={"original": True},
    )


def test_first_and_second_attempt_use_zero_padded_names(tmp_path, result):
    assert next_attempt_number(tmp_path, "current", "run", "case-a") == 1
    assert not tmp_path.joinpath("current").exists()
    first = write_attempt(tmp_path, "run", 1, result)
    assert first == tmp_path / "current/run/attempts/case-a/0001.json"
    assert next_attempt_number(tmp_path, "current", "run", "case-a") == 2
    second = write_attempt(tmp_path, "run", 2, result)
    assert second == tmp_path / "current/run/attempts/case-a/0002.json"
    assert next_attempt_number(tmp_path, "current", "run", "case-a") == 3
    for path in (first, second):
        assert path.read_bytes().endswith(b"\n")
        assert CaseResult.model_validate_json(path.read_text()) == result
        assert set(json.loads(path.read_text())) == set(CaseResult.model_fields)


def test_attempt_writes_are_exclusive_and_preserve_original(tmp_path, result):
    original = write_attempt(tmp_path, "run", 1, result)
    before = original.read_bytes(), original.stat().st_mtime_ns
    replacement = result.model_copy(update={"raw_response": {"replacement": True}})
    with pytest.raises(FileExistsError):
        write_attempt(tmp_path, "run", 1, replacement)
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before
    assert next_attempt_number(tmp_path, "current", "run", "case-a") == 2


def test_canonical_writes_are_exclusive_and_preserve_original(tmp_path, result):
    original = write_result(tmp_path, "run", result)
    before = original.read_bytes(), original.stat().st_mtime_ns
    replacement = result.model_copy(update={"raw_response": {"replacement": True}})
    with pytest.raises(FileExistsError):
        write_result(tmp_path, "run", replacement)
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before
    assert read_result(tmp_path, "current", "run", "case-a") == result


def test_error_cannot_be_written_as_canonical(tmp_path, result):
    error = result.model_copy(update={"status": ResultStatus.ERROR, "http_status": 503})
    with pytest.raises(ValueError, match="Canonical result must have status success"):
        write_result(tmp_path, "run", error)
    assert not tmp_path.joinpath("current").exists()


def test_mutated_invalid_success_is_revalidated_before_writing(tmp_path, result):
    invalid = result.model_copy(update={"http_status": 503})
    for writer, arguments in ((write_result, (tmp_path, "run", invalid)),
                              (write_attempt, (tmp_path, "run", 1, invalid))):
        with pytest.raises(ValidationError, match="Successful results require"):
            writer(*arguments)
    assert not tmp_path.joinpath("current").exists()


@pytest.mark.parametrize("name", ["1.json", "001.json", "00001.json", "0000.json", "-001.json",
                                 "0001.JSON", "0001.txt", "abcd.json", "notes.txt", "١٢٣٤.json"])
def test_malformed_history_filename_fails_without_mutation(tmp_path, name):
    directory = attempt_directory(tmp_path, "current", "run", "case-a")
    directory.mkdir(parents=True)
    path = directory / name
    path.write_bytes(b"existing fixture\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Malformed attempt history entry"):
        next_attempt_number(tmp_path, "current", "run", "case-a")
    assert path.read_bytes() == before
    assert list(directory.iterdir()) == [path]


@pytest.mark.parametrize("entry_type", ["directory", "symlink"])
def test_nonregular_attempt_entry_is_rejected(tmp_path, entry_type):
    directory = attempt_directory(tmp_path, "current", "run", "case-a")
    directory.mkdir(parents=True)
    path = directory / "0001.json"
    if entry_type == "directory":
        path.mkdir()
    else:
        path.symlink_to("missing.json")
    with pytest.raises(ValueError, match="Malformed attempt history entry"):
        next_attempt_number(tmp_path, "current", "run", "case-a")


def test_history_directory_cannot_be_a_file(tmp_path):
    directory = attempt_directory(tmp_path, "current", "run", "case-a")
    directory.parent.mkdir(parents=True)
    directory.write_bytes(b"invalid fixture")
    with pytest.raises(ValueError, match="Attempt history must be a regular directory"):
        next_attempt_number(tmp_path, "current", "run", "case-a")


def test_numbering_preserves_gaps_instead_of_reusing_them(tmp_path, result):
    first = write_attempt(tmp_path, "run", 1, result)
    third = write_attempt(tmp_path, "run", 3, result)
    before = {path: path.read_bytes() for path in (first, third)}
    number = next_attempt_number(tmp_path, "current", "run", "case-a")
    assert number == 4
    fourth = write_attempt(tmp_path, "run", number, result)
    assert fourth.name == "0004.json"
    assert not attempt_path(tmp_path, "current", "run", "case-a", 2).exists()
    assert {path: path.read_bytes() for path in (first, third)} == before


def test_four_digit_numbering_exhaustion_fails_without_reuse(tmp_path, result):
    last = write_attempt(tmp_path, "run", 9999, result)
    before = last.read_bytes()
    with pytest.raises(ValueError, match="Attempt numbering exhausted"):
        next_attempt_number(tmp_path, "current", "run", "case-a")
    assert last.read_bytes() == before


@pytest.mark.parametrize("number", [0, -1, 10000, True, 1.5, "1"])
def test_attempt_number_must_fit_positive_four_digit_format(tmp_path, number):
    with pytest.raises(ValueError, match="attempt_number must be an integer"):
        attempt_path(tmp_path, "current", "run", "case-a", number)


@pytest.mark.parametrize("run_id, case_id", [("../escape", "case-a"), ("run", "../escape"),
                                          ("", "case-a"), ("run", "a\\b"), ("run", "manifest")])
def test_attempt_helpers_reuse_canonical_component_validation(tmp_path, run_id, case_id):
    for helper, arguments in (
        (attempt_directory, (tmp_path, "current", run_id, case_id)),
        (attempt_path, (tmp_path, "current", run_id, case_id, 1)),
        (next_attempt_number, (tmp_path, "current", run_id, case_id)),
    ):
        with pytest.raises(ValueError):
            helper(*arguments)
    assert not tmp_path.joinpath("current").exists()


@pytest.mark.parametrize("target, run_id, case_id", [("candidate", "run", "case-a"),
                                                 ("current", "other-run", "case-a"),
                                                 ("current", "run", "case-b")])
def test_attempts_are_isolated_by_target_run_and_case(tmp_path, result, target, run_id, case_id):
    first = write_attempt(tmp_path, "run", 1, result)
    before = first.read_bytes()
    assert next_attempt_number(tmp_path, "current", "run", "case-a") == 2
    assert next_attempt_number(tmp_path, target, run_id, case_id) == 1
    other = result.model_copy(update={"target": target, "case_id": case_id})
    other_path = write_attempt(tmp_path, run_id, 1, other)
    assert other_path != first
    assert next_attempt_number(tmp_path, target, run_id, case_id) == 2
    assert first.read_bytes() == before


def test_existing_canonical_error_fails_instead_of_becoming_retryable(tmp_path, result):
    path = result_path(tmp_path, "current", "run", "case-a")
    path.parent.mkdir(parents=True)
    error = result.model_copy(update={"status": ResultStatus.ERROR, "http_status": 503})
    path.write_text(error.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="Canonical result must have status success"):
        read_result(tmp_path, "current", "run", "case-a")


@pytest.mark.parametrize("entry_type", ["directory", "symlink"])
def test_nonregular_canonical_file_fails(tmp_path, entry_type):
    path = result_path(tmp_path, "current", "run", "case-a")
    path.parent.mkdir(parents=True)
    if entry_type == "directory":
        path.mkdir()
    else:
        path.symlink_to("missing.json")
    with pytest.raises(ValueError, match="Canonical result must be a regular file"):
        read_result(tmp_path, "current", "run", "case-a")
