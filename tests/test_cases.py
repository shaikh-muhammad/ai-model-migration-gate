"""Synthetic in-memory validation inputs only; no benchmark records."""

import pytest
from pydantic import ValidationError

from ai_model_migration_gate.cases import Application, Case, ExpectedOutcome


@pytest.fixture
def application_data():
    return {
        "beverageType": "Wine",
        "brandName": "Test brand",
        "classType": "Table wine",
        "alcoholContent": "12%",
        "netContents": "750 mL",
        "producerName": "Test producer",
        "producerAddress": "Test address",
        "importedProduct": False,
    }


def test_non_imported_application_without_country(application_data):
    application = Application.model_validate(application_data)
    assert application.countryOfOrigin is None


def test_non_imported_application_with_null_country(application_data):
    application_data["countryOfOrigin"] = None
    assert Application.model_validate(application_data).countryOfOrigin is None


def test_imported_application_without_country_is_rejected(application_data):
    application_data["importedProduct"] = True
    with pytest.raises(ValidationError, match="countryOfOrigin"):
        Application.model_validate(application_data)


@pytest.mark.parametrize("country", [None, "", " \t\n"])
def test_imported_application_with_empty_country_is_rejected(application_data, country):
    application_data.update(importedProduct=True, countryOfOrigin=country)
    with pytest.raises(ValidationError, match="countryOfOrigin"):
        Application.model_validate(application_data)


def test_imported_application_with_country(application_data):
    application_data.update(importedProduct=True, countryOfOrigin="France")
    assert Application.model_validate(application_data).countryOfOrigin == "France"


def test_unknown_application_field_is_rejected(application_data):
    application_data["brandNmae"] = "Typo"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Application.model_validate(application_data)


@pytest.fixture
def case_data(application_data):
    return {
        "id": "validation-test",
        "image": "images/validation-test.png",
        "application": application_data,
        "expected_outcome": "Pass",
        "tags": [],
        "critical": False,
    }


@pytest.mark.parametrize("outcome", ["Pass", "Needs Review", "Fail"])
def test_case_accepts_expected_outcomes(case_data, outcome):
    case_data["expected_outcome"] = outcome
    assert Case.model_validate(case_data).expected_outcome == ExpectedOutcome(outcome)


@pytest.mark.parametrize("outcome", ["pass", "needs_review", "fail"])
def test_case_rejects_api_outcome_strings(case_data, outcome):
    case_data["expected_outcome"] = outcome
    with pytest.raises(ValidationError, match="expected_outcome"):
        Case.model_validate(case_data)


def test_unknown_case_field_is_rejected(case_data):
    case_data["critcal"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Case.model_validate(case_data)
