import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tomllib

import httpx
import pytest

import ai_model_migration_gate
from ai_model_migration_gate.cases import Case, load_cases
from ai_model_migration_gate.cli import build_parser, main
from ai_model_migration_gate.config import GateConfig, load_config
from ai_model_migration_gate import fingerprint


REPOSITORY = Path(__file__).resolve().parents[1]
IDENTITY_FIELDS = {
    "declared_model", "prompt_sha256", "verifier_commit_sha", "case_set_sha256", "tool_version",
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fingerprinting attempted networking or HTTP client construction")

    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)


def fixture_git(path, *arguments):
    """Git mutations are confined to pytest's temporary fixture repositories."""
    return subprocess.run(
        ["git", "-c", "user.name=Offline Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(path), *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def verifier(tmp_path):
    path = tmp_path / "verifier"
    path.mkdir()
    fixture_git(path, "init", "--quiet")
    prompt = path / fingerprint.PROMPT_RELATIVE_PATH
    prompt.parent.mkdir()
    prompt.write_bytes(b'export const extractionPrompt = `Evidence only.`;\n')
    (path / ".gitignore").write_text(".env*\nignored.txt\n", encoding="utf-8")
    fixture_git(path, "add", ".gitignore", str(fingerprint.PROMPT_RELATIVE_PATH))
    fixture_git(path, "commit", "--quiet", "-m", "Offline fixture")
    return path


@pytest.fixture
def case_project(tmp_path):
    project = tmp_path / "project"
    image = project / "cases/images/label.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture image bytes")
    original = load_cases(REPOSITORY / "cases/cases.jsonl")[0].model_dump(mode="json")
    records = [
        {**original, "id": "b", "image": "cases/images/label.png", "tags": ["z", "a"]},
        {**original, "id": "a", "image": "cases/images/label.png", "tags": ["two", "one"]},
    ]
    cases_path = project / "cases/cases.jsonl"
    cases_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return project / "gate.yaml", cases_path, load_cases(cases_path)


@pytest.fixture
def fingerprint_project(case_project, verifier):
    config_path, cases_path, cases = case_project
    data = {
        "targets": {
            "current": {"base_url": "http://127.0.0.1:3000", "verifier_path": "../verifier",
                        "model": "declared-test-model"},
            "candidate": {"base_url": "http://127.0.0.1:3001", "verifier_path": "../verifier",
                          "model": "CHANGE_ME"},
        },
    }
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return GateConfig.model_validate(data), config_path, cases_path, cases, verifier


def test_prompt_hashes_the_entire_source_file(verifier):
    contents = (verifier / fingerprint.PROMPT_RELATIVE_PATH).read_bytes()
    assert fingerprint.prompt_sha256(verifier) == hashlib.sha256(contents).hexdigest()
    assert fingerprint.prompt_sha256(verifier) != hashlib.sha256(b"Evidence only.").hexdigest()


def test_one_prompt_byte_changes_hash(verifier):
    prompt = verifier / fingerprint.PROMPT_RELATIVE_PATH
    before = fingerprint.prompt_sha256(verifier)
    prompt.write_bytes(prompt.read_bytes().replace(b"Evidence", b"evidence"))
    assert fingerprint.prompt_sha256(verifier) != before


def test_prompt_line_endings_are_not_normalized(verifier):
    prompt = verifier / fingerprint.PROMPT_RELATIVE_PATH
    original = prompt.read_bytes()
    before = fingerprint.prompt_sha256(verifier)
    changed = original.replace(b"\n", b"\r\n")
    prompt.write_bytes(changed)
    assert fingerprint.prompt_sha256(verifier) == hashlib.sha256(changed).hexdigest()
    assert fingerprint.prompt_sha256(verifier) != before


@pytest.mark.parametrize("directory", [False, True])
def test_missing_or_nonregular_prompt_fails(tmp_path, directory):
    if directory:
        (tmp_path / fingerprint.PROMPT_RELATIVE_PATH).mkdir(parents=True)
    with pytest.raises(ValueError, match="Prompt file.*" + ("regular file" if directory else "does not exist")):
        fingerprint.prompt_sha256(tmp_path)


def test_clean_git_repository_returns_full_commit(verifier):
    expected = fixture_git(verifier, "rev-parse", "HEAD")
    assert fingerprint.verifier_commit_sha(verifier) == expected
    assert len(expected) == 40


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked"])
def test_dirty_git_repository_is_rejected(verifier, change):
    if change == "untracked":
        (verifier / "new-file.txt").write_text("untracked", encoding="utf-8")
    else:
        prompt = verifier / fingerprint.PROMPT_RELATIVE_PATH
        prompt.write_bytes(prompt.read_bytes() + b"// changed\n")
        if change == "staged":
            fixture_git(verifier, "add", str(fingerprint.PROMPT_RELATIVE_PATH))
    with pytest.raises(ValueError, match="Verifier working tree is dirty"):
        fingerprint.verifier_commit_sha(verifier)


def test_ignored_environment_file_is_not_read_or_treated_as_dirty(
    fingerprint_project, monkeypatch,
):
    config, config_path, _, cases, verifier = fingerprint_project
    (verifier / ".env.local").write_text("OFFLINE_FIXTURE_ONLY=unused\n", encoding="utf-8")
    (verifier / "ignored.txt").write_text("ignored", encoding="utf-8")
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name.startswith(".env"):
            pytest.fail("Fingerprint calculation read an environment file")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    identity = fingerprint.calculate_fingerprint(config, "current", config_path, cases)
    assert identity.verifier_commit_sha == fixture_git(verifier, "rev-parse", "HEAD")


def test_git_commands_are_read_only_and_include_untracked_files(verifier, monkeypatch):
    original_run = subprocess.run
    commands = []

    def observe(command, **kwargs):
        commands.append(command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    fingerprint.verifier_commit_sha(verifier)
    assert len(commands) == 2
    assert all("--no-optional-locks" in command and "core.fsmonitor=false" in command for command in commands)
    assert commands[0][-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]
    assert "--ignored" not in commands[0]
    assert commands[1][-3:] == ["rev-parse", "--verify", "HEAD"]


def test_non_git_directory_fails_clearly(tmp_path):
    with pytest.raises(ValueError, match="Cannot inspect verifier Git repository"):
        fingerprint.verifier_commit_sha(tmp_path)


def test_declared_model_comes_only_from_config(fingerprint_project, monkeypatch):
    config, config_path, _, cases, _ = fingerprint_project
    monkeypatch.setenv("OPENAI_MODEL", "different-environment-model")
    identity = fingerprint.calculate_fingerprint(config, "current", config_path, cases)
    assert identity.declared_model == "declared-test-model"


@pytest.mark.parametrize("model", ["", " \t", "CHANGE_ME"])
def test_empty_or_placeholder_model_fails_before_verifier_access(
    fingerprint_project, model, monkeypatch,
):
    config, config_path, _, cases, _ = fingerprint_project
    config.targets.current.model = model

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid model caused verifier inspection")

    monkeypatch.setattr(fingerprint, "verifier_commit_sha", forbidden)
    with pytest.raises(ValueError, match="nonempty model other than CHANGE_ME"):
        fingerprint.calculate_fingerprint(config, "current", config_path, cases)


def test_tool_version_comes_from_runtime_package(fingerprint_project, monkeypatch):
    config, config_path, _, cases, _ = fingerprint_project
    monkeypatch.setattr(ai_model_migration_gate, "__version__", "fixture-version")
    identity = fingerprint.calculate_fingerprint(config, "current", config_path, cases)
    assert identity.tool_version == ai_model_migration_gate.__version__ == "fixture-version"


def test_project_version_matches_runtime_version():
    metadata = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["version"] == ai_model_migration_gate.__version__


def test_verifier_and_images_resolve_from_config_directory(fingerprint_project, monkeypatch, tmp_path):
    config, config_path, _, cases, verifier = fingerprint_project
    monkeypatch.chdir(tmp_path)
    identity = fingerprint.calculate_fingerprint(config, "current", config_path, cases)
    assert identity.verifier_commit_sha == fixture_git(verifier, "rev-parse", "HEAD")
    assert identity.prompt_sha256 == hashlib.sha256((verifier / fingerprint.PROMPT_RELATIVE_PATH).read_bytes()).hexdigest()


def test_canonical_records_have_exact_fields_and_are_sorted(case_project):
    config_path, _, cases = case_project
    records = json.loads(fingerprint.canonical_case_json(cases, config_path))
    assert [record["id"] for record in records] == ["a", "b"]
    expected_fields = {"id", "expected_outcome", "application", "critical", "tags", "image_sha256"}
    assert all(set(record) == expected_fields for record in records)
    assert records[0]["tags"] == ["one", "two"]
    assert records[1]["expected_outcome"] == "Pass"
    assert records[0]["image_sha256"] == hashlib.sha256(
        (config_path.parent / "cases/images/label.png").read_bytes()
    ).hexdigest()
    assert [case.id for case in cases] == ["b", "a"]
    assert cases[0].tags == ["z", "a"]


def test_tag_order_does_not_change_hash(case_project):
    config_path, _, cases = case_project
    before = fingerprint.case_set_sha256(cases, config_path)
    changed = [case.model_copy(update={"tags": list(reversed(case.tags))}) for case in cases]
    assert fingerprint.case_set_sha256(changed, config_path) == before


def test_jsonl_physical_order_does_not_change_hash(case_project):
    config_path, cases_path, cases = case_project
    before = fingerprint.case_set_sha256(cases, config_path)
    lines = cases_path.read_text(encoding="utf-8").splitlines()
    cases_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    reloaded = load_cases(cases_path)
    assert [case.id for case in reloaded] == ["a", "b"]
    assert fingerprint.case_set_sha256(reloaded, config_path) == before


@pytest.mark.parametrize("outcome", ["Pass", "Needs Review", "Fail"])
def test_canonical_outcomes_use_exact_human_values(case_project, outcome):
    config_path, _, cases = case_project
    data = cases[0].model_dump(mode="json")
    data["expected_outcome"] = outcome
    record = json.loads(fingerprint.canonical_case_json([Case.model_validate(data)], config_path))[0]
    assert record["expected_outcome"] == outcome


@pytest.mark.parametrize("field", ["expected_outcome", "critical", "application"])
def test_case_identity_changes_affect_hash(case_project, field):
    config_path, _, cases = case_project
    before = fingerprint.case_set_sha256(cases, config_path)
    data = cases[0].model_dump(mode="json")
    if field == "expected_outcome":
        data[field] = "Fail"
    elif field == "critical":
        data[field] = True
    else:
        data[field]["brandName"] += " "
    changed = [Case.model_validate(data), cases[1]]
    assert fingerprint.case_set_sha256(changed, config_path) != before


def test_one_image_byte_changes_hash(case_project):
    config_path, _, cases = case_project
    before = fingerprint.case_set_sha256(cases, config_path)
    image = config_path.parent / cases[0].image
    original = image.read_bytes()
    image.write_bytes(original[:-1] + b"!")
    assert fingerprint.case_set_sha256(cases, config_path) != before


@pytest.mark.parametrize("directory", [False, True])
def test_missing_or_nonregular_case_image_fails(case_project, directory):
    config_path, _, cases = case_project
    image = config_path.parent / cases[0].image
    image.unlink()
    if directory:
        image.mkdir()
    with pytest.raises(ValueError, match="Case image.*" + ("regular file" if directory else "does not exist")):
        fingerprint.case_set_sha256(cases, config_path)


def test_none_optional_application_fields_are_omitted(case_project):
    config_path, _, cases = case_project
    assert cases[0].application.countryOfOrigin is None
    application = json.loads(fingerprint.canonical_case_json(cases, config_path))[0]["application"]
    assert "countryOfOrigin" not in application


def test_application_content_is_preserved_and_json_is_canonical(case_project):
    config_path, _, cases = case_project
    data = cases[0].model_dump(mode="json")
    original = "  Café Mixed CASE!\t\n"
    data["application"]["brandName"] = original
    case = Case.model_validate(data)
    canonical = fingerprint.canonical_case_json([case], config_path)
    records = json.loads(canonical)
    assert records[0]["application"]["brandName"] == original
    assert "Café" in canonical
    assert "\\u00e9" not in canonical
    assert canonical.startswith('[{"application":{')
    assert canonical == json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert fingerprint.case_set_sha256([case], config_path) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_all_real_cases_fingerprint_with_a_temporary_verifier(verifier):
    config_path = REPOSITORY / "gate.yaml"
    config = load_config(config_path)
    config.targets.current.verifier_path = verifier
    cases = load_cases(REPOSITORY / "cases/cases.jsonl")
    identity = fingerprint.calculate_fingerprint(config, "current", config_path, cases)
    assert len(cases) == 12
    assert identity.declared_model == "gpt-5.4-mini"
    assert identity.tool_version == ai_model_migration_gate.__version__
    assert len(identity.prompt_sha256) == len(identity.case_set_sha256) == 64


def test_fingerprint_parser_defaults():
    args = build_parser().parse_args(["fingerprint", "--target", "current"])
    assert args.config == Path("gate.yaml")
    assert args.cases == Path("cases/cases.jsonl")
    assert args.json is False


def test_fingerprint_help_is_read_only(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fingerprint help attempted filesystem or Git work")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["fingerprint", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert all(option in output for option in ("--target", "--config", "--cases", "--json"))


@pytest.mark.parametrize("json_output", [False, True])
def test_fingerprint_cli_output_and_read_only_behavior(
    fingerprint_project, json_output, monkeypatch, capsys,
):
    config, config_path, cases_path, cases, verifier = fingerprint_project
    expected = fingerprint.calculate_fingerprint(config, "current", config_path, cases)
    before = {path: path.read_bytes() for path in config_path.parent.rglob("*") if path.is_file()}
    git_index_before = (verifier / ".git/index").read_bytes()
    elsewhere = verifier.parent
    monkeypatch.chdir(elsewhere)
    original_open = Path.open

    def forbid_writing(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            pytest.fail("Fingerprint CLI attempted a file write")
        return original_open(path, mode, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Fingerprint CLI attempted to create files or directories")

    monkeypatch.setattr(Path, "open", forbid_writing)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    arguments = ["fingerprint", "--target", "current", "--config", str(config_path),
                 "--cases", str(cases_path.relative_to(config_path.parent))]
    if json_output:
        arguments.append("--json")
    main(arguments)
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output) == expected.model_dump(mode="json")
        assert set(json.loads(output)) == IDENTITY_FIELDS
    else:
        assert output.splitlines() == [f"{name}: {value}" for name, value in expected.model_dump().items()]
    after = {path: path.read_bytes() for path in config_path.parent.rglob("*") if path.is_file()}
    assert before == after
    assert (verifier / ".git/index").read_bytes() == git_index_before
    assert not (config_path.parent / "results").exists()


def test_real_candidate_placeholder_rejected_without_verifier_access(monkeypatch, capsys):
    monkeypatch.chdir(REPOSITORY)

    def forbidden(*args, **kwargs):
        pytest.fail("Candidate placeholder caused verifier or image inspection")

    monkeypatch.setattr(fingerprint, "verifier_commit_sha", forbidden)
    monkeypatch.setattr(fingerprint, "file_sha256", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["fingerprint", "--target", "candidate"])
    assert error.value.code == 2
    assert "CHANGE_ME" in capsys.readouterr().err
