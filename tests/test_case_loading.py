import json
from pathlib import Path

import pytest

from ai_model_migration_gate.cases import load_cases


CORPUS = Path(__file__).resolve().parents[1] / "cases/cases.jsonl"


@pytest.fixture
def first_record():
    return json.loads(CORPUS.read_text(encoding="utf-8").splitlines()[0])


def test_real_corpus_loads_in_order():
    cases = load_cases(CORPUS)
    assert len(cases) == 12
    assert [case.id[:2] for case in cases] == [f"{number:02}" for number in range(1, 13)]
    assert cases[0].id == "01-perfect"
    assert cases[-1].id == "12-country-mismatch"


def test_blank_lines_are_ignored_and_file_order_is_preserved(tmp_path, first_record):
    second_record = {**first_record, "id": "second"}
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n" + json.dumps(second_record) + "\n \t\n" + json.dumps(first_record) + "\n",
        encoding="utf-8",
    )
    assert [case.id for case in load_cases(path)] == ["second", "01-perfect"]


def test_duplicate_ids_report_the_line(tmp_path, first_record):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(first_record) + "\n\n" + json.dumps(first_record), encoding="utf-8")
    with pytest.raises(ValueError, match="line 3: duplicate case ID '01-perfect'"):
        load_cases(path)


@pytest.mark.parametrize(
    "bad_line, message", [("{", "malformed JSON"), ('{"id": "incomplete"}', "invalid case data")]
)
def test_invalid_records_report_the_source_line(tmp_path, first_record, bad_line, message):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(first_record) + "\n\n" + bad_line, encoding="utf-8")
    with pytest.raises(ValueError, match=f"line 3: {message}") as error:
        load_cases(path)
    assert str(path) in str(error.value)


def test_loading_does_not_inspect_image_or_verifier_paths(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Loading cases attempted filesystem inspection")

    monkeypatch.setattr(Path, "stat", forbidden)
    assert len(load_cases(CORPUS)) == 12
