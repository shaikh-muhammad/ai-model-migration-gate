"""Case models and JSONL loading without verifier access."""

from enum import Enum
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class ExpectedOutcome(str, Enum):
    PASS = "Pass"
    NEEDS_REVIEW = "Needs Review"
    FAIL = "Fail"


class Application(BaseModel):
    """The application fields accepted by the verifier request schema."""

    model_config = ConfigDict(extra="forbid")

    beverageType: str
    brandName: str
    classType: str
    alcoholContent: str
    netContents: str
    producerName: str
    producerAddress: str
    importedProduct: bool
    countryOfOrigin: str | None = None

    @model_validator(mode="after")
    def require_country_for_imports(self) -> "Application":
        if self.importedProduct and (
            self.countryOfOrigin is None or not self.countryOfOrigin.strip()
        ):
            raise ValueError("countryOfOrigin is required for imported products")
        return self


class Case(BaseModel):
    """One case record using the human-readable expected outcome values."""

    model_config = ConfigDict(extra="forbid")

    id: str
    image: Path
    application: Application
    expected_outcome: ExpectedOutcome
    tags: list[str]
    critical: bool


def load_cases(path: str | Path = "cases/cases.jsonl") -> list[Case]:
    """Validate nonblank JSONL records, rejecting duplicates and preserving order."""
    cases = []
    seen_ids = set()
    with Path(path).open(encoding="utf-8") as case_file:
        for line_number, line in enumerate(case_file, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}: line {line_number}: malformed JSON: {exc.msg}"
                ) from exc
            try:
                case = Case.model_validate(data)
            except ValidationError as exc:
                raise ValueError(
                    f"{path}: line {line_number}: invalid case data: {exc}"
                ) from exc
            if case.id in seen_ids:
                raise ValueError(
                    f"{path}: line {line_number}: duplicate case ID {case.id!r}"
                )
            seen_ids.add(case.id)
            cases.append(case)
    return cases
