"""Offline integrity checks for the approved synthetic corpus."""

from collections import Counter
import json
from pathlib import Path

import pytest

from ai_model_migration_gate.cases import Case


REPOSITORY = Path(__file__).resolve().parents[1]
EXPECTED_CASES = [
    ("01-perfect", "Pass", ["clean-match"], False),
    ("02-wrong-abv", "Fail", ["alcohol-content"], True),
    ("03-wrong-volume", "Fail", ["net-contents"], True),
    ("04-brand-case-only", "Pass", ["brand", "case-variation"], False),
    ("05-warning-title-case", "Fail", ["warning", "case-sensitivity"], True),
    ("06-warning-word-changed", "Fail", ["warning", "wording"], True),
    ("07-warning-missing", "Fail", ["warning", "missing"], True),
    ("08-warning-not-bold", "Fail", ["warning", "formatting"], True),
    ("09-glare", "Needs Review", ["warning", "glare", "image-quality"], True),
    ("10-angled", "Pass", ["image-quality", "angle"], False),
    ("11-imported-pass", "Pass", ["clean-match", "imported"], False),
    ("12-country-mismatch", "Fail", ["imported", "country-of-origin"], True),
]


@pytest.fixture
def records():
    lines = (REPOSITORY / "cases/cases.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 12
    assert all(line.strip() for line in lines)
    return [json.loads(line) for line in lines]


def test_records_validate_with_unique_ordered_ids(records):
    expected_fields = {"id", "image", "application", "expected_outcome", "tags", "critical"}
    cases = []
    for record in records:
        assert set(record) == expected_fields
        cases.append(Case.model_validate(record))
    ids = [case.id for case in cases]
    assert len(set(ids)) == 12
    assert ids == [case_id for case_id, _, _, _ in EXPECTED_CASES]


def test_image_references_are_relative_and_exist(records):
    references = [record["image"] for record in records]
    assert len(references) == len(set(references)) == 12
    for record in records:
        path = Path(record["image"])
        assert not path.is_absolute()
        assert record["image"] == f"cases/images/{record['id']}.png"
        assert (REPOSITORY / path).is_file()
    assert {path.name for path in (REPOSITORY / "cases/images").glob("*.png")} == {
        Path(reference).name for reference in references
    }


def test_final_ground_truth(records):
    assert [
        (record["id"], record["expected_outcome"], record["tags"], record["critical"])
        for record in records
    ] == EXPECTED_CASES
    assert Counter(record["expected_outcome"] for record in records) == {
        "Pass": 4, "Needs Review": 1, "Fail": 7,
    }
    assert sum(record["critical"] is True for record in records) == 8
    by_id = {record["id"]: record for record in records}
    assert by_id["04-brand-case-only"]["expected_outcome"] == "Pass"
    assert by_id["10-angled"]["expected_outcome"] == "Pass"
    assert by_id["09-glare"]["expected_outcome"] == "Needs Review"


def test_application_records(records):
    application_a = {
        "beverageType": "distilled-spirits",
        "brandName": "OLD TOM DISTILLERY",
        "classType": "Kentucky Straight Bourbon Whiskey",
        "alcoholContent": "45% ABV",
        "netContents": "750 mL",
        "producerName": "Old Tom Distillery LLC",
        "producerAddress": "123 Bourbon Lane, Lexington, KY 40507",
        "importedProduct": False,
    }
    for record in records[:10]:
        assert record["application"] == application_a
        assert "countryOfOrigin" not in record["application"]
        application = Case.model_validate(record).application
        assert application.importedProduct is False
        assert application.countryOfOrigin is None

    application_b = {**application_a, "importedProduct": True, "countryOfOrigin": "Canada"}
    for record in records[10:]:
        assert record["application"] == application_b
        application = Case.model_validate(record).application
        assert application.importedProduct is True
        assert application.countryOfOrigin == "Canada"
