"""Scoring saved temporary results without images, verifier clones, or Git."""

import json
import math
from pathlib import Path
import subprocess

import httpx
import pytest
from pydantic import ValidationError

from ai_model_migration_gate import cli, fingerprint, runner, scoring
from ai_model_migration_gate.cases import ExpectedOutcome, load_cases
from ai_model_migration_gate.manifest import RunManifest, manifest_path
from ai_model_migration_gate.results import CaseResult, ResultStatus, result_path, write_attempt, write_result
from ai_model_migration_gate.scoring import (
    Classification, RunScoringError, ScoredCase, ScoredRun, actual_outcome,
    aggregate_scores, response_time_statistics, score_case, score_run,
)


ROOT = Path(__file__).resolve().parents[1]
RAW_OUTCOMES = {"Pass": "pass", "Needs Review": "needs_review", "Fail": "fail"}


@pytest.fixture(autouse=True)
def forbid_external_scoring_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline scoring attempted verifier, Git, client, or fingerprint work")

    monkeypatch.setattr(fingerprint, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(fingerprint, "_git", forbidden)
    monkeypatch.setattr(cli, "calculate_fingerprint", forbidden)
    monkeypatch.setattr(runner, "preflight_case", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)


@pytest.fixture
def case():
    return load_cases(ROOT / "cases/cases.jsonl")[0]


def successful_result(case_id, actual="Pass", response_time_ms=100.0):
    return CaseResult(
        case_id=case_id, target="current", status="success", http_status=200,
        response_time_ms=response_time_ms,
        raw_response={"verification": {"overall": {"status": RAW_OUTCOMES[actual]}}},
    )


@pytest.mark.parametrize("raw, expected", [("pass", "Pass"), ("needs_review", "Needs Review"), ("fail", "Fail")])
def test_exact_raw_outcome_mapping(raw, expected):
    assert actual_outcome({"verification": {"overall": {"status": raw}}}).value == expected


@pytest.mark.parametrize("raw", [None, [], "pass", 1, True, {}, {"verification": None},
    {"verification": []}, {"verification": "wrong"}, {"verification": {}},
    {"verification": {"overall": None}}, {"verification": {"overall": []}},
    {"verification": {"overall": "pass"}}, {"verification": {"overall": {}}},
    *[{"verification": {"overall": {"status": value}}} for value in
      (None, 1, True, [], {}, "Pass", "PASS", "needs review", "Needs Review", "fail ", "", "unknown")],
])
def test_malformed_missing_or_nonexact_raw_outcomes_are_rejected(raw):
    with pytest.raises(ValueError, match="raw_response.verification"):
        actual_outcome(raw)


@pytest.mark.parametrize("expected, actual, classification, rank", [
    ("Pass", "Pass", "correct", 0),
    ("Pass", "Needs Review", "minor", 1),
    ("Pass", "Fail", "false_block", 2),
    ("Needs Review", "Pass", "dangerous", 3),
    ("Needs Review", "Needs Review", "correct", 0),
    ("Needs Review", "Fail", "false_block", 2),
    ("Fail", "Pass", "dangerous", 3),
    ("Fail", "Needs Review", "minor", 1),
    ("Fail", "Fail", "correct", 0),
])
def test_all_nine_frozen_classifications_and_severity_ranks(case, expected, actual, classification, rank):
    case = case.model_copy(update={"expected_outcome": ExpectedOutcome(expected)})
    score = score_case(case, successful_result(case.id, actual))
    assert score.expected_outcome.value == expected
    assert score.actual_outcome.value == actual
    assert score.classification.value == classification
    assert score.severity_rank == rank
    assert (score.classification == Classification.DANGEROUS) == (
        expected in ("Fail", "Needs Review") and actual == "Pass"
    )
    assert score.critical == case.critical
    assert score.tags == case.tags
    assert score.response_time_ms == 100.0


def test_critical_and_noncritical_dangerous_counts_and_accuracy(case):
    scores = []
    for expected, actual, critical in [
        ("Fail", "Pass", True), ("Needs Review", "Pass", False),
        ("Pass", "Fail", True), ("Pass", "Pass", True), ("Fail", "Needs Review", True),
    ]:
        variant = case.model_copy(update={"expected_outcome": ExpectedOutcome(expected), "critical": critical})
        scores.append(score_case(variant, successful_result(case.id, actual)))
    metrics = aggregate_scores(scores)
    assert metrics.total_cases == 5
    assert metrics.correct_cases == 1
    assert metrics.minor_cases == 1
    assert metrics.false_block_cases == 1
    assert metrics.dangerous_cases == 2
    assert metrics.critical_dangerous_cases == 1
    assert metrics.accuracy == 1 / 5


@pytest.mark.parametrize("field, value", [("extra", "unused"), ("critical", "yes"),
    ("severity_rank", True), ("severity_rank", 4), ("response_time_ms", float("inf")),
    ("response_time_ms", float("nan")), ("response_time_ms", -1), ("classification", "correct")])
def test_scored_case_model_is_strict(case, field, value):
    data = score_case(case, successful_result(case.id)).model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        ScoredCase.model_validate(data)


def test_inconsistent_classification_or_rank_is_rejected(case):
    data = score_case(case, successful_result(case.id)).model_dump()
    for changes in ({"severity_rank": 3}, {"classification": Classification.DANGEROUS}):
        with pytest.raises(ValidationError, match="frozen outcome matrix"):
            ScoredCase.model_validate({**data, **changes})


@pytest.mark.parametrize("change", [{"case_id": "other"}, {"status": ResultStatus.ERROR}])
def test_case_scoring_requires_matching_success(case, change):
    with pytest.raises(ValueError, match="matching canonical successful"):
        score_case(case, successful_result(case.id).model_copy(update=change))


@pytest.mark.parametrize("values, expected", [([3], (3, 3)), ([10, 1, 4], (4, 10)),
    ([4, 1, 3, 2], (2.5, 4)), (list(range(1, 21)), (10.5, 19)),
    (list(range(1, 22)), (11, 20)), (list(range(1, 101)), (50.5, 95))])
def test_standard_median_and_nearest_rank_p95(values, expected):
    assert response_time_statistics(values) == expected
    assert response_time_statistics(values)[1] == sorted(values)[math.ceil(0.95 * len(values)) - 1]


def test_twelve_observation_p95_is_the_maximum_without_interpolation():
    values = [1000, 1, 11, 6, 2, 5, 9, 3, 10, 8, 4, 7]
    assert len(values) == 12
    assert math.ceil(0.95 * len(values)) == 12
    median_ms, p95_ms = response_time_statistics(values)
    assert median_ms == 6.5
    assert p95_ms == max(values) == 1000


@pytest.mark.parametrize("values", [[], [float("inf")], [float("nan")], [-1]])
def test_invalid_timing_samples_rejected(values):
    with pytest.raises(ValueError, match="finite nonnegative"):
        response_time_statistics(values)


def test_complete_saved_twelve_case_run_scores_without_verifier_or_images(saved_run):
    report = score_run(**saved_run)
    assert report.metrics.total_cases == report.metrics.correct_cases == 12
    assert report.metrics.minor_cases == report.metrics.false_block_cases == 0
    assert report.metrics.dangerous_cases == report.metrics.critical_dangerous_cases == 0
    assert report.metrics.accuracy == 1.0
    assert report.metrics.median_response_time_ms == 650.0
    assert report.metrics.p95_response_time_ms == 1200.0
    assert [case.case_id for case in report.cases] == sorted(case.id for case in saved_run["cases"])
    assert report.target == "current" and report.run_id == saved_run["run_id"]
    assert ScoredRun.model_validate_json(report.model_dump_json()) == report
    assert not (saved_run["results_dir"].parent / "cases/images").exists()
    assert not (saved_run["results_dir"].parent.parent / "absent-verifier").exists()


def test_missing_canonical_case_returns_structured_problems_without_metrics(saved_run, monkeypatch):
    case = saved_run["cases"][0]
    result_path(saved_run["results_dir"], "current", saved_run["run_id"], case.id).unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("An incomplete run computed aggregate metrics")

    monkeypatch.setattr(scoring, "aggregate_scores", forbidden)
    with pytest.raises(RunScoringError, match="Missing canonical cases") as error:
        score_run(**saved_run)
    assert error.value.problems.missing_case_ids == [case.id]
    assert "metrics" not in error.value.problems.model_dump()
    assert not hasattr(error.value, "metrics")


@pytest.mark.parametrize("substitute", ["attempt_error", "other_run", "other_target"])
def test_attempts_or_other_run_target_results_cannot_satisfy_completeness(saved_run, substitute):
    case = saved_run["cases"][0]
    result_path(saved_run["results_dir"], "current", saved_run["run_id"], case.id).unlink()
    result = successful_result(case.id)
    if substitute == "attempt_error":
        write_attempt(saved_run["results_dir"], saved_run["run_id"], 1,
                      result.model_copy(update={"status": ResultStatus.ERROR, "http_status": 503}))
    elif substitute == "other_run":
        write_result(saved_run["results_dir"], "another-run", result)
    else:
        write_result(saved_run["results_dir"], saved_run["run_id"], result.model_copy(update={"target": "candidate"}))
    with pytest.raises(RunScoringError) as error:
        score_run(**saved_run)
    assert error.value.problems.missing_case_ids == [case.id]


def test_attempt_history_is_ignored_even_when_corrupt_or_more_recent(saved_run, monkeypatch):
    baseline = score_run(**saved_run)
    case = saved_run["cases"][0]
    path = write_attempt(saved_run["results_dir"], saved_run["run_id"], 1,
                         successful_result(case.id, "Fail"))
    (path.parent / "malformed-name.json").write_bytes(b"{ invalid history")
    original_open = Path.open

    def forbidden_history(path, *args, **kwargs):
        assert "attempts" not in path.parts
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbidden_history)
    assert score_run(**saved_run) == baseline


@pytest.mark.parametrize("name", ["unknown-case.json", "metrics.json", "summary.json", ".hidden.json"])
def test_every_unexpected_root_json_is_rejected_without_reading_it(saved_run, name, monkeypatch):
    root = manifest_path(saved_run["results_dir"], "current", saved_run["run_id"]).parent
    unknown = root / name
    unknown.write_bytes(b"unread fixture payload")
    original_open = Path.open

    def guard_unknown(path, *args, **kwargs):
        assert path != unknown
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_unknown)
    with pytest.raises(RunScoringError, match="Unexpected canonical files") as error:
        score_run(**saved_run)
    assert error.value.problems.unexpected_canonical_files == [name]


@pytest.mark.parametrize("invalid", ["json", "encoding", "case_id", "target", "error", "directory", "infinite_time"])
def test_invalid_canonical_results_are_identified(saved_run, invalid):
    case = saved_run["cases"][0]
    path = result_path(saved_run["results_dir"], "current", saved_run["run_id"], case.id)
    if invalid == "json":
        path.write_bytes(b"{")
    elif invalid == "encoding":
        path.write_bytes(b"\xff")
    elif invalid == "directory":
        path.unlink()
        path.mkdir()
    else:
        data = successful_result(case.id).model_dump(mode="json")
        if invalid == "case_id":
            data["case_id"] = "another-case"
        elif invalid == "target":
            data["target"] = "candidate"
        elif invalid == "error":
            data["status"], data["http_status"] = "error", 503
        else:
            data["response_time_ms"] = float("inf")
        path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RunScoringError, match="Invalid canonical results") as error:
        score_run(**saved_run)
    assert list(error.value.problems.invalid_case_results) == [case.id]


def test_multiple_case_problems_are_collected_without_aggregate_metrics(saved_run):
    missing, corrupt, outcome = saved_run["cases"][:3]
    root = manifest_path(saved_run["results_dir"], "current", saved_run["run_id"]).parent
    (root / f"{missing.id}.json").unlink()
    (root / f"{corrupt.id}.json").write_bytes(b"{")
    data = successful_result(outcome.id).model_dump(mode="json")
    data["raw_response"] = {"verification": {"overall": {"status": "PRIVATE_PAYLOAD_MARKER"}}}
    (root / f"{outcome.id}.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "unexpected.json").write_bytes(b"unread")
    with pytest.raises(RunScoringError) as error:
        score_run(**saved_run)
    problems = error.value.problems
    assert problems.missing_case_ids == [missing.id]
    assert list(problems.invalid_case_results) == [corrupt.id]
    assert list(problems.invalid_outcomes) == [outcome.id]
    assert problems.unexpected_canonical_files == ["unexpected.json"]
    assert "PRIVATE_PAYLOAD_MARKER" not in str(error.value)


@pytest.mark.parametrize("invalid", ["missing", "corrupt", "encoding", "run_id", "target", "null_pin", "fingerprint"])
def test_manifest_and_pin_problems_fail_before_canonical_outcomes(saved_run, invalid, monkeypatch):
    path = manifest_path(saved_run["results_dir"], "current", saved_run["run_id"])
    data = json.loads(path.read_text())
    if invalid == "missing":
        path.unlink()
    elif invalid == "corrupt":
        path.write_bytes(b"{")
    elif invalid == "encoding":
        path.write_bytes(b"\xff")
    elif invalid == "null_pin":
        saved_run["config"].targets.current.expected_fingerprint = None
    else:
        if invalid == "fingerprint":
            data["fingerprint"]["prompt_sha256"] = "changed"
            data["fingerprint"]["verifier_commit_sha"] = "changed"
        else:
            data[invalid] = "another-run" if invalid == "run_id" else "candidate"
        path.write_text(json.dumps(data), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid manifest/pin reached canonical scoring")

    monkeypatch.setattr(scoring, "read_result", forbidden)
    with pytest.raises(RunScoringError) as error:
        score_run(**saved_run)
    assert error.value.problems.manifest_errors
    if invalid == "fingerprint":
        assert error.value.problems.manifest_errors == ["Fingerprint mismatch: prompt_sha256, verifier_commit_sha"]


@pytest.mark.parametrize("run_id", ["../escape", "", "a\\b"])
def test_scoring_reuses_safe_run_path_rules(saved_run, run_id):
    with pytest.raises(RunScoringError, match="filename component"):
        score_run(**{**saved_run, "run_id": run_id})


def test_empty_or_duplicate_corpus_is_invalid(saved_run):
    for cases in ([], [saved_run["cases"][0]] * 2):
        with pytest.raises(RunScoringError, match="Corpus problems"):
            score_run(**{**saved_run, "cases": cases})


def test_offline_scoring_reads_only_canonical_files_and_writes_nothing(saved_run, monkeypatch):
    root = manifest_path(saved_run["results_dir"], "current", saved_run["run_id"]).parent
    allowed = {path for path in root.iterdir() if path.is_file()}
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in allowed}
    original_open = Path.open
    original_stat = Path.stat

    def guard_open(path, mode="r", *args, **kwargs):
        assert path in allowed
        assert not any(flag in mode for flag in ("w", "a", "x", "+"))
        return original_open(path, mode, *args, **kwargs)

    def guard_stat(path, *args, **kwargs):
        assert "absent-verifier" not in path.parts
        assert path.suffix != ".png"
        return original_stat(path, *args, **kwargs)

    def forbidden_write(*args, **kwargs):
        pytest.fail("Scoring attempted a filesystem write")

    monkeypatch.setattr(Path, "open", guard_open)
    monkeypatch.setattr(Path, "stat", guard_stat)
    monkeypatch.setattr(Path, "mkdir", forbidden_write)
    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    assert score_run(**saved_run).metrics.total_cases == 12
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in allowed} == before
