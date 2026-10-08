from email import policy
from email.parser import BytesParser
import json
import math
from pathlib import Path
import socket

import httpx
import pytest
import yaml

import ai_model_migration_gate
from ai_model_migration_gate import cli, runner
from ai_model_migration_gate import fingerprint
from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import load_config
from ai_model_migration_gate.manifest import RunManifest, manifest_path, prepare_run_manifest
from ai_model_migration_gate.results import (
    CaseResult, ResultStatus, attempt_directory, attempt_path, next_attempt_number,
    read_result, result_path, write_attempt, write_result,
)
from ai_model_migration_gate.runner import REQUEST_TIMEOUT_SECONDS, execute_plan, plan_run


HTTPX_CLIENT = httpx.Client
RUN_ID = "mock-run"


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Execution tests attempted real networking or a real HTTP transport")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)


@pytest.fixture
def project(offline_run_project):
    return offline_run_project


@pytest.fixture
def cases(project):
    return load_cases(project / "cases/cases.jsonl")


@pytest.fixture
def plan(project, cases):
    return plan_run(
        cases, target="current", max_calls=3, results_dir=project / "results",
        run_id=RUN_ID, yes=True,
    )


@pytest.fixture
def arguments(project, cases):
    return {
        "config": load_config(project / "gate.yaml"), "cases": cases,
        "config_path": project / "gate.yaml", "results_dir": project / "results",
    }


class MockClientFactory:
    """Capture client settings and requests while always using MockTransport."""

    def __init__(self, handler=None):
        self.handler = handler
        self.settings = []
        self.clients = []
        self.requests = []

    def __call__(self, **settings):
        self.settings.append(settings)

        def handle(request):
            self.requests.append(request)
            if self.handler is not None:
                return self.handler(request)
            return httpx.Response(200, json={"processingTimeMs": 3})

        client = HTTPX_CLIENT(transport=httpx.MockTransport(handle), **settings)
        self.clients.append(client)
        return client


def request_case_id(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.read()
    )
    image = next(part for part in message.iter_parts() if part.get_filename() is not None)
    return Path(image.get_filename()).stem


def test_execution_requires_confirmation_before_client_creation(plan, arguments):
    plan.confirmed = False
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="Explicit confirmation"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == []
    assert not arguments["results_dir"].exists()


def test_execution_requires_run_id_before_client_creation(plan, arguments):
    plan.run_id = None
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="--run-id is required with --yes"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == []
    assert not arguments["results_dir"].exists()


def test_zero_selected_cases_validate_manifest_without_client_or_case_results(plan, arguments, capsys):
    plan.selected_cases = []
    plan.skipped_case_ids = ["01-perfect"]
    factory = MockClientFactory()
    summary = execute_plan(plan, client_factory=factory, **arguments)
    assert summary.attempted_count == 0
    assert summary.successful_attempt_count == 0
    assert summary.error_attempt_count == 0
    assert summary.skipped_success_count == 1
    assert factory.clients == []
    path = manifest_path(arguments["results_dir"], "current", RUN_ID)
    assert path.is_file()
    assert list(path.parent.iterdir()) == [path]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("invalid_index", [0, 1, 2])
def test_entire_batch_is_preflighted_before_any_manifest_client_or_call(
    plan, arguments, invalid_index, monkeypatch,
):
    plan.selected_cases[invalid_index] = plan.selected_cases[invalid_index].model_copy(
        update={"image": Path("cases/images/missing.png")},
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Batch preflight failure reached manifest preparation")

    monkeypatch.setattr(runner, "prepare_run_manifest", forbidden)
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="Image does not exist"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == []
    assert factory.requests == []
    assert not arguments["results_dir"].exists()


def test_one_client_is_reused_with_explicit_confirmation_and_closed(plan, arguments, monkeypatch, capsys):
    factory = MockClientFactory()
    original_call = runner.call_case
    clients_seen = []

    def record_call(case, **kwargs):
        assert kwargs["confirmed"] is True
        clients_seen.append(kwargs["client"])
        return original_call(case, **kwargs)

    monkeypatch.setattr(runner, "call_case", record_call)
    summary = execute_plan(plan, client_factory=factory, **arguments)
    assert len(factory.clients) == 1
    assert clients_seen == [factory.clients[0]] * 3
    assert factory.clients[0].is_closed
    assert len(factory.requests) == summary.attempted_count == 3
    assert summary.successful_attempt_count == 3
    assert summary.error_attempt_count == 0
    assert capsys.readouterr().out == ""


def test_client_settings_use_finite_named_timeout_and_no_environment_or_redirects(plan, arguments):
    factory = MockClientFactory()
    execute_plan(plan, client_factory=factory, **arguments)
    assert REQUEST_TIMEOUT_SECONDS == 30.0
    assert math.isfinite(REQUEST_TIMEOUT_SECONDS)
    assert factory.settings == [{
        "trust_env": False, "follow_redirects": False, "timeout": REQUEST_TIMEOUT_SECONDS,
    }]
    client = factory.clients[0]
    assert client.trust_env is False
    assert client.follow_redirects is False
    for timeout in (client.timeout.connect, client.timeout.read, client.timeout.write, client.timeout.pool):
        assert timeout == REQUEST_TIMEOUT_SECONDS


def test_cases_execute_in_preserved_case_order(plan, arguments):
    plan.selected_cases = list(reversed(plan.selected_cases))
    factory = MockClientFactory()
    summary = execute_plan(plan, client_factory=factory, **arguments)
    expected_ids = [case.id for case in plan.selected_cases]
    assert [request_case_id(request) for request in factory.requests] == expected_ids
    assert [attempt.result.case_id for attempt in summary.attempts] == expected_ids


def test_success_http_error_and_transport_error_are_saved_immediately(plan, arguments):
    recorded = []

    def handler(request):
        number = len(factory.requests)
        for previous in plan.selected_cases[:number - 1]:
            previous_path = attempt_path(arguments["results_dir"], "current", RUN_ID, previous.id, 1)
            assert previous_path.is_file()
        if number == 1:
            return httpx.Response(200, json={"processingTimeMs": 3})
        if number == 2:
            return httpx.Response(503, json={"code": "SYNTHETIC_UNAVAILABLE"})
        raise httpx.ConnectError("Synthetic connection failure", request=request)

    def on_recorded(attempt):
        assert attempt.attempt_path.is_file()
        assert CaseResult.model_validate_json(attempt.attempt_path.read_text()) == attempt.result
        canonical = read_result(arguments["results_dir"], "current", RUN_ID, attempt.result.case_id)
        assert canonical == (attempt.result if attempt.result.status == ResultStatus.SUCCESS else None)
        recorded.append(attempt)

    factory = MockClientFactory(handler)
    summary = execute_plan(plan, client_factory=factory, on_recorded=on_recorded, **arguments)
    assert summary.attempts == recorded
    assert summary.attempted_count == 3
    assert summary.successful_attempt_count == 1
    assert summary.error_attempt_count == 2
    for case, attempt in zip(plan.selected_cases, summary.attempts):
        assert attempt.attempt_number == 1
        assert attempt.attempt_path == (
            arguments["results_dir"] / "current" / RUN_ID / "attempts" / case.id / "0001.json"
        )
        assert attempt.canonical_path == (
            result_path(arguments["results_dir"], "current", RUN_ID, case.id)
            if attempt.result.status == ResultStatus.SUCCESS else None
        )
    success, http_error, transport_error = [attempt.result for attempt in summary.attempts]
    assert success.status == ResultStatus.SUCCESS
    assert success.http_status == 200
    assert http_error.status == ResultStatus.ERROR
    assert http_error.http_status == 503
    assert http_error.error_code == "SYNTHETIC_UNAVAILABLE"
    assert transport_error.status == ResultStatus.ERROR
    assert transport_error.http_status is None
    assert transport_error.raw_response is None
    assert transport_error.error_message == "Synthetic connection failure"


def test_interruption_preserves_success_and_resumes_same_run(plan, cases, arguments):
    def handler(request):
        if len(first_factory.requests) == 2:
            raise KeyboardInterrupt("Synthetic interruption")
        return httpx.Response(200, json={"processingTimeMs": 3})

    first_factory = MockClientFactory(handler)
    with pytest.raises(KeyboardInterrupt, match="Synthetic interruption"):
        execute_plan(plan, client_factory=first_factory, **arguments)
    assert first_factory.clients[0].is_closed
    assert read_result(arguments["results_dir"], "current", RUN_ID, cases[0].id).status == ResultStatus.SUCCESS
    assert read_result(arguments["results_dir"], "current", RUN_ID, cases[1].id) is None
    assert read_result(arguments["results_dir"], "current", RUN_ID, cases[2].id) is None
    resumed = plan_run(
        cases, target="current", max_calls=2, results_dir=arguments["results_dir"],
        run_id=RUN_ID, yes=True,
    )
    assert resumed.selected_cases == cases[1:3]
    assert resumed.skipped_case_ids == [cases[0].id]
    second_factory = MockClientFactory()
    summary = execute_plan(resumed, client_factory=second_factory, **arguments)
    assert [request_case_id(request) for request in second_factory.requests] == [case.id for case in cases[1:3]]
    assert summary.attempted_count == summary.successful_attempt_count == 2
    assert summary.skipped_success_count == 1
    assert second_factory.clients[0].is_closed


def test_existing_error_can_be_retried_in_same_run(cases, arguments):
    def handler(request):
        if len(factory.requests) == 1:
            return httpx.Response(503, json={"code": "SYNTHETIC_ERROR"})
        return httpx.Response(200, json={"processingTimeMs": 3})

    factory = MockClientFactory(handler)
    first_plan = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        run_id=RUN_ID, case_id="01-perfect", yes=True,
    )
    assert execute_plan(first_plan, client_factory=factory, **arguments).error_attempt_count == 1
    first_path = attempt_path(arguments["results_dir"], "current", RUN_ID, "01-perfect", 1)
    first_bytes = first_path.read_bytes()
    assert read_result(arguments["results_dir"], "current", RUN_ID, "01-perfect") is None
    retry_plan = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        run_id=RUN_ID, case_id="01-perfect", yes=True,
    )
    assert retry_plan.selected_cases == [cases[0]]
    summary = execute_plan(retry_plan, client_factory=factory, **arguments)
    assert summary.successful_attempt_count == 1
    assert len(factory.requests) == len(factory.clients) == 2
    assert all(client.is_closed for client in factory.clients)
    assert read_result(arguments["results_dir"], "current", RUN_ID, "01-perfect").status == ResultStatus.SUCCESS
    assert first_path.read_bytes() == first_bytes
    attempt = summary.attempts[0]
    assert attempt.attempt_number == 2
    assert attempt.attempt_path.name == "0002.json"
    assert attempt.canonical_path.read_bytes() == attempt.attempt_path.read_bytes()


def test_force_can_execute_an_existing_success(cases, arguments):
    prepare_run_manifest(target="current", run_id=RUN_ID, **arguments)
    canonical = write_result(arguments["results_dir"], RUN_ID, CaseResult(
        case_id="01-perfect", target="current", status="success",
        http_status=200, response_time_ms=1, raw_response={"old": True},
    ))
    before = canonical.read_bytes(), canonical.stat().st_mtime_ns
    skipped_plan = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        run_id=RUN_ID, case_id="01-perfect", yes=True,
    )
    factory = MockClientFactory()
    assert execute_plan(skipped_plan, client_factory=factory, **arguments).skipped_success_count == 1
    assert factory.clients == []
    forced_plan = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        run_id=RUN_ID, case_id="01-perfect", force=True, yes=True,
    )
    summary = execute_plan(forced_plan, client_factory=factory, **arguments)
    assert summary.attempted_count == 1
    assert summary.attempts[0].canonical_path is None
    assert len(factory.requests) == 1
    assert read_result(arguments["results_dir"], "current", RUN_ID, "01-perfect").raw_response == {"old": True}
    assert (canonical.read_bytes(), canonical.stat().st_mtime_ns) == before


def test_redirect_response_is_recorded_without_following_it(plan, arguments):
    plan.selected_cases = plan.selected_cases[:1]
    factory = MockClientFactory(lambda request: httpx.Response(
        307, headers={"location": "http://other.invalid"}, json={"code": "SYNTHETIC_REDIRECT"},
    ))
    summary = execute_plan(plan, client_factory=factory, **arguments)
    assert len(factory.requests) == 1
    assert summary.error_attempt_count == 1
    assert summary.attempts[0].result.http_status == 307


@pytest.mark.parametrize("budget, case_id", [(3, None), (1, None), (1, "01-perfect")])
def test_confirmed_cli_execution_is_mocked_capped_and_printed(project, cases, budget, case_id, monkeypatch, capsys):
    factory = MockClientFactory()
    monkeypatch.setattr(cli.httpx, "Client", factory)
    monkeypatch.chdir(project)
    arguments = ["run", "--target", "current", "--max-calls", str(budget), "--run-id", RUN_ID, "--yes"]
    if case_id is not None:
        arguments.extend(["--case", case_id])
    cli.main(arguments)
    expected = cases[:budget]
    assert [request_case_id(request) for request in factory.requests] == [case.id for case in expected]
    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed
    output = capsys.readouterr().out
    for case in expected:
        assert f"{case.id}: success; HTTP=200; response_time_ms=" in output
        assert str(project / "results/current" / RUN_ID / f"{case.id}.json") in output
        assert str(project / "results/current" / RUN_ID / "attempts" / case.id / "0001.json") in output
    assert output.count("attempt_number=1;") == budget
    assert output.count("canonical_result_file=") == budget
    assert f"Run summary: attempted={budget}; successful_attempts={budget}; error_attempts=0; skipped_successes=0" in output


def test_confirmed_cli_with_no_eligible_case_creates_no_client(project, monkeypatch, capsys):
    prepare_run_manifest(
        config=load_config(project / "gate.yaml"), target="current", run_id=RUN_ID,
        config_path=project / "gate.yaml", cases=load_cases(project / "cases/cases.jsonl"),
        results_dir=project / "results",
    )
    result_file = write_result(project / "results", RUN_ID, CaseResult(
        case_id="01-perfect", target="current", status="success",
        http_status=200, response_time_ms=1, raw_response={},
    ))
    original = result_file.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("A confirmed empty run constructed a client")

    monkeypatch.setattr(cli.httpx, "Client", forbidden)
    monkeypatch.chdir(project)
    cli.main([
        "run", "--target", "current", "--max-calls", "1", "--case", "01-perfect",
        "--run-id", RUN_ID, "--yes",
    ])
    output = capsys.readouterr().out
    assert "Nothing to run: no eligible cases." in output
    assert "attempted=0; successful_attempts=0; error_attempts=0; skipped_successes=1" in output
    assert result_file.read_bytes() == original


def test_confirmed_cli_preflight_error_makes_zero_calls_and_writes(project, monkeypatch, capsys):
    config = project / "gate.yaml"
    records = load_cases(project / "cases/cases.jsonl")
    records[1].image = Path("cases/images/missing.png")
    (project / "cases/cases.jsonl").write_text(
        "\n".join(case.model_dump_json(exclude_none=True) for case in records) + "\n", encoding="utf-8",
    )
    factory = MockClientFactory()
    monkeypatch.setattr(cli.httpx, "Client", factory)
    with pytest.raises(SystemExit) as result:
        cli.main([
            "run", "--target", "current", "--config", str(config),
            "--max-calls", "3", "--run-id", RUN_ID, "--yes",
        ])
    assert result.value.code == 2
    assert "Image does not exist" in capsys.readouterr().err
    assert factory.clients == []
    assert factory.requests == []
    assert not (project / "results").exists()


def test_confirmed_run_creates_manifest_before_mock_client_and_first_attempt(plan, arguments, monkeypatch):
    path = manifest_path(arguments["results_dir"], "current", RUN_ID)
    events = []
    preflight = runner.preflight_case
    result_path = runner.result_path
    prepare = runner.prepare_run_manifest

    def observe_preflight(case, config_path):
        events.append(("preflight", case.id))
        return preflight(case, config_path)

    def observe_result_path(results_dir, target, run_id, case_id):
        if case_id != "validation":
            events.append(("path", case_id))
        return result_path(results_dir, target, run_id, case_id)

    def observe_manifest(**kwargs):
        events.append(("manifest", None))
        return prepare(**kwargs)

    def handler(request):
        assert path.is_file()
        assert RunManifest.model_validate_json(path.read_text()).fingerprint == (
            arguments["config"].targets.current.expected_fingerprint
        )
        return httpx.Response(200, json={"mocked": True})

    factory = MockClientFactory(handler)

    def guarded_factory(**settings):
        events.append(("client", None))
        assert path.is_file()
        return factory(**settings)

    monkeypatch.setattr(runner, "preflight_case", observe_preflight)
    monkeypatch.setattr(runner, "result_path", observe_result_path)
    monkeypatch.setattr(runner, "prepare_run_manifest", observe_manifest)
    assert not path.exists()
    summary = execute_plan(plan, client_factory=guarded_factory, **arguments)
    expected = [("preflight", case.id) for case in plan.selected_cases]
    expected += [("path", case.id) for case in plan.selected_cases]
    expected += [("manifest", None), ("client", None)]
    assert events[:len(expected)] == expected
    assert len(factory.clients) == 1
    assert summary.attempted_count == 3


def test_resume_reuses_manifest_bytes_before_mock_client(plan, arguments, cases):
    plan.selected_cases = plan.selected_cases[:1]
    first_factory = MockClientFactory()
    execute_plan(plan, client_factory=first_factory, **arguments)
    path = manifest_path(arguments["results_dir"], "current", RUN_ID)
    before = path.read_bytes(), path.stat().st_mtime_ns
    resumed = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        run_id=RUN_ID, yes=True,
    )
    second_factory = MockClientFactory()

    def guarded_factory(**settings):
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        return second_factory(**settings)

    summary = execute_plan(resumed, client_factory=guarded_factory, **arguments)
    assert summary.skipped_success_count == 1
    assert summary.attempted_count == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert len(second_factory.clients) == 1


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("field", ["declared_model", "prompt_sha256", "verifier_commit_sha",
                                 "case_set_sha256", "tool_version"])
def test_each_actual_fingerprint_drift_blocks_client_creation(
    plan, arguments, field, resume, temporary_git, monkeypatch,
):
    path = manifest_path(arguments["results_dir"], "current", RUN_ID)
    if resume:
        prepare_run_manifest(target="current", run_id=RUN_ID, **arguments)
    before = (path.read_bytes(), path.stat().st_mtime_ns) if resume else None
    verifier = arguments["config_path"].parent.parent / "verifier"
    expected_message = "Fingerprint mismatch: " + field
    if field == "declared_model":
        arguments["config"].targets.current.model = "different-model"
    elif field == "prompt_sha256":
        prompt = verifier / fingerprint.PROMPT_RELATIVE_PATH
        prompt.write_bytes(prompt.read_bytes() + b"// drift\n")
        temporary_git(verifier, "add", str(fingerprint.PROMPT_RELATIVE_PATH))
        temporary_git(verifier, "commit", "--quiet", "-m", "Prompt drift fixture")
        expected_message += ", verifier_commit_sha"
    elif field == "verifier_commit_sha":
        temporary_git(verifier, "commit", "--quiet", "--allow-empty", "-m", "Commit drift fixture")
    elif field == "case_set_sha256":
        # Drift outside the selected batch must still affect the full run identity.
        image = arguments["config_path"].parent / arguments["cases"][-1].image
        image.write_bytes(image.read_bytes() + b"image drift")
    else:
        monkeypatch.setattr(ai_model_migration_gate, "__version__", "different-version")
    factory = MockClientFactory()
    with pytest.raises(ValueError) as error:
        execute_plan(plan, client_factory=factory, **arguments)
    assert str(error.value) == expected_message
    assert factory.clients == factory.requests == []
    if resume:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    else:
        assert not arguments["results_dir"].exists()


@pytest.mark.parametrize("field", ["run_id", "target", "fingerprint"])
def test_manifest_mismatches_block_client_creation(plan, arguments, field):
    manifest = prepare_run_manifest(target="current", run_id=RUN_ID, **arguments)
    path = manifest_path(arguments["results_dir"], "current", RUN_ID)
    data = manifest.model_dump(mode="json")
    if field == "fingerprint":
        data[field]["declared_model"] = "different-model"
        message = "Fingerprint mismatch: declared_model"
    else:
        data[field] = "other-run" if field == "run_id" else "candidate"
        message = f"Manifest {field} does not match"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    factory = MockClientFactory()
    with pytest.raises(ValueError, match=message):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == factory.requests == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("empty_selection", [False, True])
def test_null_expected_pin_blocks_even_empty_confirmed_execution(plan, arguments, empty_selection):
    arguments["config"].targets.current.expected_fingerprint = None
    if empty_selection:
        plan.selected_cases = []
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="non-null expected_fingerprint"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == factory.requests == []
    assert not arguments["results_dir"].exists()


@pytest.mark.parametrize("force", [False, True])
def test_confirmed_pre_manifest_run_is_rejected_even_when_success_is_skipped(cases, arguments, force):
    result_file = write_result(arguments["results_dir"], RUN_ID, CaseResult(
        case_id=cases[0].id, target="current", status="success",
        http_status=200, response_time_ms=1, raw_response={},
    ))
    before = result_file.read_bytes()
    plan = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        case_id=cases[0].id, run_id=RUN_ID, yes=True, force=force,
    )
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="Pre-manifest or invalid run.*new run ID"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == factory.requests == []
    assert result_file.read_bytes() == before
    assert not manifest_path(arguments["results_dir"], "current", RUN_ID).exists()


def test_all_skipped_resume_still_checks_for_drift(cases, arguments):
    plan = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        case_id=cases[0].id, run_id=RUN_ID, yes=True,
    )
    execute_plan(plan, client_factory=MockClientFactory(), **arguments)
    path = manifest_path(arguments["results_dir"], "current", RUN_ID)
    before = path.read_bytes()
    arguments["config"].targets.current.model = "changed"
    resumed = plan_run(
        cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
        case_id=cases[0].id, run_id=RUN_ID, yes=True,
    )
    assert resumed.selected_cases == []
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="Fingerprint mismatch: declared_model"):
        execute_plan(resumed, client_factory=factory, **arguments)
    assert factory.clients == []
    assert path.read_bytes() == before


def test_invalid_result_path_fails_after_all_preflight_without_manifest_or_client(plan, arguments):
    plan.selected_cases[-1] = plan.selected_cases[-1].model_copy(update={"id": "manifest"})
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="reserved"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == []
    assert not arguments["results_dir"].exists()


@pytest.mark.parametrize("target", ["current", "candidate"])
def test_confirmed_cli_null_pin_fails_before_client_or_verifier_access(
    project, target, monkeypatch, capsys,
):
    config_path = project / "gate.yaml"
    config = load_config(config_path)
    getattr(config.targets, target).expected_fingerprint = None
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    factory = MockClientFactory()
    monkeypatch.setattr(cli.httpx, "Client", factory)

    def forbidden(*args, **kwargs):
        pytest.fail("Null pin must fail before verifier inspection")

    monkeypatch.setattr(fingerprint, "verifier_commit_sha", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "--target", target, "--config", str(config_path),
                  "--max-calls", "1", "--run-id", RUN_ID, "--yes"])
    assert error.value.code == 2
    assert "non-null expected_fingerprint" in capsys.readouterr().err
    assert factory.clients == []
    assert not (project / "results").exists()
    if target == "candidate":
        assert config.targets.candidate.model == "CHANGE_ME"


def test_every_success_writes_attempt_before_canonical_immediately(plan, arguments, monkeypatch):
    events = []
    original_attempt = runner.write_attempt
    original_canonical = runner.write_result

    def handler(request):
        case_id = request_case_id(request)
        for previous in plan.selected_cases[:len(factory.requests) - 1]:
            assert attempt_path(arguments["results_dir"], "current", RUN_ID, previous.id, 1).is_file()
            assert read_result(arguments["results_dir"], "current", RUN_ID, previous.id) is not None
        events.append((case_id, "response"))
        return httpx.Response(200, json={"case": case_id})

    def observe_attempt(results_dir, run_id, number, result):
        assert read_result(results_dir, result.target, run_id, result.case_id) is None
        path = original_attempt(results_dir, run_id, number, result)
        events.append((result.case_id, "attempt"))
        return path

    def observe_canonical(results_dir, run_id, result):
        history = attempt_path(results_dir, result.target, run_id, result.case_id, 1)
        assert CaseResult.model_validate_json(history.read_text()) == result
        path = original_canonical(results_dir, run_id, result)
        assert path.read_bytes() == history.read_bytes()
        events.append((result.case_id, "canonical"))
        return path

    monkeypatch.setattr(runner, "write_attempt", observe_attempt)
    monkeypatch.setattr(runner, "write_result", observe_canonical)
    factory = MockClientFactory(handler)
    execute_plan(plan, client_factory=factory, **arguments)
    assert events == [(case.id, event) for case in plan.selected_cases
                      for event in ("response", "attempt", "canonical")]


def test_attempt_write_collision_preserves_existing_attempt_without_canonical(plan, arguments):
    plan.selected_cases = plan.selected_cases[:1]
    existing = CaseResult(
        case_id=plan.selected_cases[0].id, target="current", status="success",
        http_status=200, response_time_ms=1, raw_response={"competing": True},
    )

    def handler(request):
        write_attempt(arguments["results_dir"], RUN_ID, 1, existing)
        return httpx.Response(200, json={"replacement": True})

    factory = MockClientFactory(handler)
    with pytest.raises(FileExistsError):
        execute_plan(plan, client_factory=factory, **arguments)
    path = attempt_path(arguments["results_dir"], "current", RUN_ID, existing.case_id, 1)
    assert CaseResult.model_validate_json(path.read_text()) == existing
    assert read_result(arguments["results_dir"], "current", RUN_ID, existing.case_id) is None
    assert len(factory.requests) == 1
    assert factory.clients[0].is_closed


def test_canonical_creation_collision_preserves_attempt_and_existing_success(plan, arguments, monkeypatch):
    plan.selected_cases = plan.selected_cases[:1]
    original_write = runner.write_result
    competing = CaseResult(
        case_id=plan.selected_cases[0].id, target="current", status="success",
        http_status=200, response_time_ms=1, raw_response={"competing": True},
    )

    def collide(results_dir, run_id, result):
        assert attempt_path(results_dir, result.target, run_id, result.case_id, 1).is_file()
        original_write(results_dir, run_id, competing)
        return original_write(results_dir, run_id, result)

    monkeypatch.setattr(runner, "write_result", collide)
    factory = MockClientFactory()
    with pytest.raises(FileExistsError):
        execute_plan(plan, client_factory=factory, **arguments)
    assert read_result(arguments["results_dir"], "current", RUN_ID, competing.case_id) == competing
    history = attempt_path(arguments["results_dir"], "current", RUN_ID, competing.case_id, 1)
    assert CaseResult.model_validate_json(history.read_text()).raw_response == {"processingTimeMs": 3}
    assert len(factory.requests) == 1


def test_multiple_forced_success_http_error_and_transport_error_preserve_canonical(cases, arguments):
    def case_plan(force=False):
        return plan_run(
            cases, target="current", max_calls=1, results_dir=arguments["results_dir"],
            run_id=RUN_ID, case_id=cases[0].id, yes=True, force=force,
        )

    first = execute_plan(case_plan(), client_factory=MockClientFactory(), **arguments).attempts[0]
    canonical = first.canonical_path
    before = canonical.read_bytes(), canonical.stat().st_mtime_ns
    manifest = manifest_path(arguments["results_dir"], "current", RUN_ID)
    manifest_before = manifest.read_bytes(), manifest.stat().st_mtime_ns
    history_before = {first.attempt_path: first.attempt_path.read_bytes()}

    def handler(request):
        if len(factory.requests) == 1:
            return httpx.Response(200, json={"forced": True})
        if len(factory.requests) == 2:
            return httpx.Response(503, json={"code": "FORCED_HTTP_ERROR"})
        raise httpx.ConnectError("Forced transport error", request=request)

    factory = MockClientFactory(handler)
    for number in (2, 3, 4):
        recorded = execute_plan(case_plan(force=True), client_factory=factory, **arguments).attempts[0]
        assert recorded.attempt_number == number
        assert recorded.attempt_path.name == f"{number:04d}.json"
        assert recorded.canonical_path is None
        assert (canonical.read_bytes(), canonical.stat().st_mtime_ns) == before
        assert {path: path.read_bytes() for path in history_before} == history_before
        history_before[recorded.attempt_path] = recorded.attempt_path.read_bytes()
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == manifest_before
    assert next_attempt_number(arguments["results_dir"], "current", RUN_ID, cases[0].id) == 5
    normal = case_plan()
    assert normal.selected_cases == []
    assert normal.skipped_case_ids == [cases[0].id]
    unused_factory = MockClientFactory()
    assert execute_plan(normal, client_factory=unused_factory, **arguments).attempted_count == 0
    assert unused_factory.clients == []


def test_interruption_preserves_prior_successes_errors_and_resume_retries_only_errors(plan, cases, arguments):
    def handler(request):
        return httpx.Response(
            503 if len(factory.requests) == 2 else 200,
            json={"case": request_case_id(request)},
        )

    recorded = []

    def interrupt_after_third(attempt):
        recorded.append(attempt)
        if len(recorded) == 3:
            raise KeyboardInterrupt("Interruption after persisted case N")

    factory = MockClientFactory(handler)
    with pytest.raises(KeyboardInterrupt, match="persisted case N"):
        execute_plan(plan, client_factory=factory, on_recorded=interrupt_after_third, **arguments)
    assert factory.clients[0].is_closed
    assert len(recorded) == 3
    assert all(attempt.attempt_path.is_file() for attempt in recorded)
    assert recorded[0].canonical_path.is_file()
    assert recorded[1].canonical_path is None
    assert recorded[2].canonical_path.is_file()
    before = {path: path.read_bytes() for path in arguments["results_dir"].rglob("*") if path.is_file()}
    resumed = plan_run(
        cases, target="current", max_calls=2, results_dir=arguments["results_dir"],
        run_id=RUN_ID, yes=True,
    )
    assert resumed.selected_cases == [cases[1], cases[3]]
    assert resumed.skipped_case_ids == [cases[0].id, cases[2].id]
    resumed_factory = MockClientFactory()
    summary = execute_plan(resumed, client_factory=resumed_factory, **arguments)
    assert summary.successful_attempt_count == 2
    assert [attempt.attempt_number for attempt in summary.attempts] == [2, 1]
    assert summary.attempts[0].canonical_path.read_bytes() == summary.attempts[0].attempt_path.read_bytes()
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("invalid", ["corrupt", "case_mismatch", "target_mismatch", "error"])
def test_canonical_integrity_blocks_normal_and_forced_cli_before_execution(
    project, cases, arguments, invalid, force, monkeypatch, capsys,
):
    prepare_run_manifest(target="current", run_id=RUN_ID, **arguments)
    path = result_path(arguments["results_dir"], "current", RUN_ID, cases[0].id)
    result = CaseResult(case_id=cases[0].id, target="current", status="success",
                        http_status=200, response_time_ms=1, raw_response={})
    if invalid == "corrupt":
        payload, message = "{", "Invalid JSON"
    else:
        changes = {
            "case_mismatch": {"case_id": "wrong-case"},
            "target_mismatch": {"target": "candidate"},
            "error": {"status": ResultStatus.ERROR, "http_status": 503},
        }
        payload = result.model_copy(update=changes[invalid]).model_dump_json()
        message = "status success" if invalid == "error" else "identity does not match"
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()
    factory = MockClientFactory()
    monkeypatch.setattr(cli.httpx, "Client", factory)
    options = ["run", "--target", "current", "--config", str(project / "gate.yaml"),
               "--max-calls", "1", "--case", cases[0].id, "--run-id", RUN_ID, "--yes"]
    if force:
        options.append("--force")
    with pytest.raises(SystemExit) as error:
        cli.main(options)
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert factory.clients == factory.requests == []
    assert path.read_bytes() == before
    assert not attempt_directory(arguments["results_dir"], "current", RUN_ID, cases[0].id).exists()


def test_execution_rechecks_canonical_integrity_after_planning(plan, arguments):
    prepare_run_manifest(target="current", run_id=RUN_ID, **arguments)
    path = result_path(arguments["results_dir"], "current", RUN_ID, plan.selected_cases[1].id)
    path.write_text("{", encoding="utf-8")
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="Invalid JSON"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == factory.requests == []
    assert not (path.parent / "attempts").exists()


def test_malformed_history_fails_before_any_client_or_attempt(plan, arguments):
    prepare_run_manifest(target="current", run_id=RUN_ID, **arguments)
    directory = attempt_directory(arguments["results_dir"], "current", RUN_ID, plan.selected_cases[1].id)
    directory.mkdir(parents=True)
    malformed = directory / "1.json"
    malformed.write_bytes(b"existing malformed fixture")
    factory = MockClientFactory()
    with pytest.raises(ValueError, match="Malformed attempt history entry"):
        execute_plan(plan, client_factory=factory, **arguments)
    assert factory.clients == factory.requests == []
    assert malformed.read_bytes() == b"existing malformed fixture"
    assert not attempt_directory(arguments["results_dir"], "current", RUN_ID, plan.selected_cases[0].id).exists()


@pytest.mark.parametrize("transport_error", [False, True])
def test_cli_error_reports_attempt_path_without_canonical(project, cases, monkeypatch, capsys, transport_error):
    def handler(request):
        if transport_error:
            raise httpx.ConnectError("Mocked transport error", request=request)
        return httpx.Response(503, json={"code": "MOCKED_HTTP_ERROR"})

    factory = MockClientFactory(handler)
    monkeypatch.setattr(cli.httpx, "Client", factory)
    cli.main(["run", "--target", "current", "--config", str(project / "gate.yaml"),
              "--max-calls", "1", "--run-id", RUN_ID, "--yes"])
    output = capsys.readouterr().out
    path = attempt_path(project / "results", "current", RUN_ID, cases[0].id, 1)
    assert f"attempt_number=1; attempt_file={path}" in output
    assert ("HTTP=n/a" if transport_error else "HTTP=503") in output
    assert "canonical_result_file=" not in output
    assert path.is_file()
    assert read_result(project / "results", "current", RUN_ID, cases[0].id) is None


def test_forced_cli_reports_only_new_attempt_and_preserves_canonical(project, cases, monkeypatch, capsys):
    factory = MockClientFactory()
    monkeypatch.setattr(cli.httpx, "Client", factory)
    options = ["run", "--target", "current", "--config", str(project / "gate.yaml"),
               "--max-calls", "1", "--case", cases[0].id, "--run-id", RUN_ID, "--yes"]
    cli.main(options)
    capsys.readouterr()
    canonical = result_path(project / "results", "current", RUN_ID, cases[0].id)
    before = canonical.read_bytes()
    cli.main(options + ["--force"])
    output = capsys.readouterr().out
    path = attempt_path(project / "results", "current", RUN_ID, cases[0].id, 2)
    assert f"attempt_number=2; attempt_file={path}" in output
    assert "canonical_result_file=" not in output
    assert canonical.read_bytes() == before
    assert path.is_file()
