from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import Target
from ai_model_migration_gate.results import CaseResult, ResultStatus, read_result, write_attempt, write_result
from ai_model_migration_gate import runner
from ai_model_migration_gate.runner import (
    MAX_APPLICATION_CHARACTERS, MAX_IMAGE_BYTES,
    call_case, plan_run, preflight_case, resolve_image_path,
)


ROOT = Path(__file__).resolve().parents[1]
IMAGE_HEADERS = {
    ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG\r\n\x1a\n", ".webp": b"RIFF\x04\x00\x00\x00WEBP",
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Runner tests attempted a real network operation")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def cases():
    return load_cases(ROOT / "cases/cases.jsonl")


@pytest.fixture
def arguments(cases):
    return {
        "case": cases[0], "target": "current", "config_path": ROOT / "gate.yaml",
        "target_config": Target(
            base_url="http://verifier.invalid:3000", verifier_path="../not-inspected", model="TEST",
        ),
        "confirmed": True,
    }


@pytest.fixture
def temporary_case(tmp_path, cases):
    image = tmp_path / "image.png"
    image.write_bytes(IMAGE_HEADERS[".png"])
    return cases[0].model_copy(update={"image": Path("image.png")}), tmp_path / "gate.yaml"


def test_image_paths_resolve_from_config_root_even_with_another_working_directory(cases, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_image_path(cases[0], ROOT / "gate.yaml") == ROOT / "cases/images/01-perfect.png"


def test_all_corpus_images_pass_preflight(cases):
    for case in cases:
        prepared = preflight_case(case, ROOT / "gate.yaml")
        assert prepared.mime_type == "image/png"
        assert json.loads(prepared.application_json) == case.application.model_dump(exclude_none=True)


def test_missing_image_is_rejected(temporary_case):
    case, config = temporary_case
    case = case.model_copy(update={"image": Path("missing.png")})
    with pytest.raises(ValueError, match="Image does not exist"):
        preflight_case(case, config)


def test_directory_is_not_an_image(temporary_case, tmp_path):
    case, config = temporary_case
    (tmp_path / "directory.png").mkdir()
    case = case.model_copy(update={"image": Path("directory.png")})
    with pytest.raises(ValueError, match="regular file"):
        preflight_case(case, config)


def test_unsupported_image_extension_is_rejected(temporary_case, tmp_path):
    case, config = temporary_case
    (tmp_path / "image.gif").write_bytes(b"GIF89a")
    case = case.model_copy(update={"image": Path("image.gif")})
    with pytest.raises(ValueError, match=".jpg, .jpeg, .png, or .webp"):
        preflight_case(case, config)


def test_mislabeled_image_content_is_rejected(temporary_case, tmp_path):
    case, config = temporary_case
    (tmp_path / "image.png").write_bytes(b"This is not a PNG")
    with pytest.raises(ValueError, match="content does not match"):
        preflight_case(case, config)


@pytest.mark.parametrize("extra_byte", [0, 1])
def test_image_size_boundary(temporary_case, tmp_path, extra_byte):
    case, config = temporary_case
    header = IMAGE_HEADERS[".png"]
    (tmp_path / "image.png").write_bytes(header + b"\0" * (MAX_IMAGE_BYTES + extra_byte - len(header)))
    if extra_byte:
        with pytest.raises(ValueError, match="exceeds 2097152 bytes"):
            preflight_case(case, config)
    else:
        assert preflight_case(case, config).image_path.stat().st_size == MAX_IMAGE_BYTES


@pytest.mark.parametrize("extra_character", [0, 1])
def test_application_json_size_boundary(temporary_case, extra_character):
    case, config = temporary_case
    original_size = len(preflight_case(case, config).application_json)
    brand = case.application.brandName + "x" * (MAX_APPLICATION_CHARACTERS + extra_character - original_size)
    application = case.application.model_copy(update={"brandName": brand})
    case = case.model_copy(update={"application": application})
    if extra_character:
        with pytest.raises(ValueError, match="exceeds 16000 characters"):
            preflight_case(case, config)
    else:
        assert len(preflight_case(case, config).application_json) == MAX_APPLICATION_CHARACTERS


@pytest.mark.parametrize(
    "extension, mime_type", [(".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
                             (".png", "image/png"), (".webp", "image/webp")]
)
def test_exact_multipart_request_and_mime_type(arguments, tmp_path, extension, mime_type):
    filename = "image" + extension
    image_bytes = IMAGE_HEADERS[extension]
    (tmp_path / filename).write_bytes(image_bytes)
    arguments["case"] = arguments["case"].model_copy(update={"image": Path(filename)})
    arguments["config_path"] = tmp_path / "gate.yaml"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/verify"
        assert request.url.host == "verifier.invalid"
        assert request.url.port == 3000
        assert request.headers["content-type"].startswith("multipart/form-data;")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode()
            + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.read()
        )
        parts = list(message.iter_parts())
        names = [part.get_param("name", header="content-disposition") for part in parts]
        assert len(parts) == 2
        assert set(names) == {"image", "application"}
        assert not {"id", "case_id", "expected_outcome", "tags", "critical"}.intersection(names)
        by_name = dict(zip(names, parts))
        assert by_name["image"].get_filename() == filename
        assert by_name["image"].get_content_type() == mime_type
        assert by_name["image"].get_payload(decode=True) == image_bytes
        application = json.loads(by_name["application"].get_payload(decode=True))
        assert application == arguments["case"].application.model_dump(exclude_none=True)
        assert set(application) == {
            "beverageType", "brandName", "classType", "alcoholContent", "netContents",
            "producerName", "producerAddress", "importedProduct",
        }
        return httpx.Response(200, json={"processingTimeMs": 7})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        assert call_case(client=client, **arguments).status == ResultStatus.SUCCESS
    assert len(requests) == 1


def test_success_retains_raw_outcome_and_uses_client_timing(arguments, monkeypatch, tmp_path):
    payload = {"verification": {"overall": {"status": "fail"}}, "processingTimeMs": 9000}
    clock = iter([10.0, 10.125])
    monkeypatch.setattr(runner, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        trust_env=False,
    ) as client:
        result = call_case(client=client, **arguments)
    assert result.status == ResultStatus.SUCCESS
    assert result.http_status == 200
    assert result.raw_response == payload
    assert result.response_time_ms == pytest.approx(125)
    assert result.error_code is None
    path = write_attempt(tmp_path, "mock-run", 1, result)
    assert CaseResult.model_validate_json(path.read_text()) == result
    write_result(tmp_path, "mock-run", result)
    assert read_result(tmp_path, "current", "mock-run", result.case_id) == result


@pytest.mark.parametrize(
    "payload", [{"code": "TEST_ERROR"}, {"error_code": "TEST_ERROR"},
                {"errorCode": "TEST_ERROR"}, {"error": {"code": "TEST_ERROR"}}]
)
def test_http_error_and_error_code_are_recorded(arguments, payload, tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json=payload)),
        trust_env=False,
    ) as client:
        result = call_case(client=client, **arguments)
    assert result.status == ResultStatus.ERROR
    assert result.http_status == 503
    assert result.raw_response == payload
    assert result.error_code == "TEST_ERROR"
    assert "expected_outcome" not in result.model_dump()
    path = write_attempt(tmp_path, "mock-run", 1, result)
    assert CaseResult.model_validate_json(path.read_text()) == result
    assert read_result(tmp_path, "current", "mock-run", result.case_id) is None


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_failure_has_no_invented_http_status(arguments, monkeypatch, exception_type):
    def handler(request):
        raise exception_type("Synthetic transport failure", request=request)

    clock = iter([10.0, 10.25])
    monkeypatch.setattr(runner, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = call_case(client=client, **arguments)
    assert result.status == ResultStatus.ERROR
    assert result.http_status is None
    assert result.raw_response is None
    assert result.error_code is None
    assert result.response_time_ms == pytest.approx(250)
    assert result.error_message == "Synthetic transport failure"


@pytest.mark.parametrize("http_status", [200, 503])
def test_non_json_response_is_an_error_with_raw_text(arguments, http_status):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(http_status, text="Synthetic non-JSON")),
        trust_env=False,
    ) as client:
        result = call_case(client=client, **arguments)
    assert result.status == ResultStatus.ERROR
    assert result.http_status == http_status
    assert result.raw_response == "Synthetic non-JSON"
    assert result.error_message == "Response was not valid JSON."


def test_redirects_do_not_create_additional_calls(arguments):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://other.invalid"}, json={"code": "REDIRECT"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, trust_env=False) as client:
        result = call_case(client=client, **arguments)
    assert len(requests) == 1
    assert result.status == ResultStatus.ERROR
    assert result.http_status == 302


def test_call_requires_explicit_confirmation(arguments):
    def forbidden(request):
        pytest.fail("An unconfirmed call reached the transport")

    del arguments["confirmed"]
    with httpx.Client(transport=httpx.MockTransport(forbidden), trust_env=False) as client:
        with pytest.raises(ValueError, match="Explicit confirmation"):
            call_case(client=client, **arguments)


def test_failed_preflight_never_reaches_transport(arguments):
    def forbidden(request):
        pytest.fail("An invalid image reached the transport")

    arguments["case"] = arguments["case"].model_copy(update={"image": Path("missing.png")})
    with httpx.Client(transport=httpx.MockTransport(forbidden), trust_env=False) as client:
        with pytest.raises(ValueError, match="Image does not exist"):
            call_case(client=client, **arguments)


def test_known_case_selection(cases, tmp_path):
    plan = plan_run(cases, target="current", max_calls=1, results_dir=tmp_path, case_id="09-glare")
    assert [case.id for case in plan.selected_cases] == ["09-glare"]
    assert plan.confirmed is False


def test_unknown_case_selection_fails(cases, tmp_path):
    with pytest.raises(ValueError, match="Unknown case ID: unknown"):
        plan_run(cases, target="current", max_calls=1, results_dir=tmp_path, case_id="unknown")


@pytest.mark.parametrize("budget", [1, 3, 12, 20])
def test_max_calls_caps_first_eligible_cases(cases, tmp_path, budget):
    plan = plan_run(cases, target="current", max_calls=budget, results_dir=tmp_path, yes=True)
    assert plan.selected_cases == cases[:budget]
    assert len(plan.selected_cases) <= budget
    assert plan.confirmed is True


@pytest.mark.parametrize("budget", [0, -1])
def test_invalid_call_budget_is_rejected(cases, tmp_path, budget):
    with pytest.raises(ValueError, match="positive integer"):
        plan_run(cases, target="current", max_calls=budget, results_dir=tmp_path)


@pytest.mark.parametrize(
    "status, force, selected_index", [("success", False, 1), ("error", False, 0), ("success", True, 0)]
)
def test_same_run_skip_retry_and_force(cases, tmp_path, status, force, selected_index):
    result = CaseResult(
        case_id=cases[0].id, target="current", status=status, response_time_ms=1,
        http_status=200 if status == "success" else None,
        raw_response={} if status == "success" else None,
    )
    write_attempt(tmp_path, "same-run", 1, result)
    if status == "success":
        write_result(tmp_path, "same-run", result)
    plan = plan_run(cases, target="current", max_calls=1, results_dir=tmp_path, run_id="same-run", force=force)
    assert plan.selected_cases == [cases[selected_index]]
    assert plan.skipped_case_ids == ([cases[0].id] if selected_index == 1 else [])


def test_budget_is_applied_after_successful_cases_are_skipped(cases, tmp_path):
    for case in cases[:3]:
        write_result(tmp_path, "same-run", CaseResult(
            case_id=case.id, target="current", status="success", response_time_ms=1,
            http_status=200, raw_response={},
        ))
    plan = plan_run(cases, target="current", max_calls=3, results_dir=tmp_path, run_id="same-run")
    assert plan.selected_cases == cases[3:6]


def test_success_in_another_run_does_not_cause_a_skip(cases, tmp_path):
    write_result(tmp_path, "old-run", CaseResult(
        case_id=cases[0].id, target="current", status="success", response_time_ms=1,
        http_status=200, raw_response={},
    ))
    plan = plan_run(cases, target="current", max_calls=1, results_dir=tmp_path, run_id="new-run")
    assert plan.selected_cases == [cases[0]]


def test_success_in_another_target_does_not_cause_a_skip(cases, tmp_path):
    write_result(tmp_path, "same-run", CaseResult(
        case_id=cases[0].id, target="candidate", status="success", response_time_ms=1,
        http_status=200, raw_response={},
    ))
    plan = plan_run(cases, target="current", max_calls=1, results_dir=tmp_path, run_id="same-run")
    assert plan.selected_cases == [cases[0]]


def test_no_run_id_disables_resume_inspection(cases, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Planning without a run ID inspected existing results")

    monkeypatch.setattr(runner, "read_result", forbidden)
    plan = plan_run(cases, target="current", max_calls=1, results_dir=tmp_path / "results")
    assert plan.run_id is None
    assert plan.selected_cases == [cases[0]]
    assert not (tmp_path / "results").exists()


def test_malformed_success_cannot_cause_resume_to_skip_a_case(cases, tmp_path):
    path = runner.result_path(tmp_path, "current", "same-run", cases[0].id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "case_id": cases[0].id, "target": "current", "status": "success",
        "response_time_ms": 1,
    }), encoding="utf-8")
    with pytest.raises(ValidationError, match="http_status in the HTTP 2xx range"):
        plan_run(cases, target="current", max_calls=1, results_dir=tmp_path, run_id="same-run")
