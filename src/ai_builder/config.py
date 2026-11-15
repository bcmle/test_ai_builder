import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .domain import Contract


class Scenario(Contract):
    team_size: int = Field(gt=0)
    business_budget_usd: Decimal = Field(gt=0)
    salaries_included: bool
    days: Literal[90]
    existing_customers: bool
    jurisdiction: str | None


class Limits(Contract):
    run_cost_usd: Decimal = Field(gt=0, le=25)
    active_seconds: int = Field(gt=0)
    provider_requests: int = Field(gt=0)
    search_requests: int = Field(gt=0)
    document_fetches: int = Field(gt=0)
    concurrent_tasks: int = Field(ge=1, le=2)


class Discovery(Contract):
    goal: int = Field(ge=1)
    cap: int = Field(ge=1)
    target: int = Field(ge=10, le=15)

    @model_validator(mode="after")
    def coherent(self):
        if self.cap < self.goal or self.goal < self.target:
            raise ValueError("Discovery target <= goal <= cap required")
        return self


class CriterionConfig(Contract):
    id: str
    name: str
    pass_rule: str
    fail_rule: str
    unknown_rule: str


class FactorConfig(Contract):
    id: str
    weight: Decimal = Field(ge=0)
    anchors: list[str] = Field(min_length=5, max_length=5)


class Configuration(Contract):
    schema_version: Literal["1.0"]
    language: Literal["en"]
    model: str = Field(min_length=1)
    market: str
    scenario: Scenario
    limits: Limits
    discovery: Discovery
    criteria: list[CriterionConfig]
    factors: list[FactorConfig]

    @model_validator(mode="after")
    def invariants(self):
        if self.market in {"RU", "BY", "AM", "AZ", "KZ", "KG", "MD", "TJ", "TM", "UZ"}:
            raise ValueError("Excluded market")
        if len(self.criteria) != 10 or {c.id for c in self.criteria} != {f"F{i:02}" for i in range(1, 11)}:
            raise ValueError("All ten mandatory filters must be configured exactly once")
        if len(self.factors) != 5 or {f.id for f in self.factors} != {f"S{i:02}" for i in range(1, 6)}:
            raise ValueError("All five scoring factors required")
        if sum(f.weight for f in self.factors) != 100:
            raise ValueError("Weights must sum to 100")
        return self

    def snapshot(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.snapshot().encode()).hexdigest()


def load_config(path: str | Path) -> Configuration:
    return Configuration.model_validate_json(Path(path).read_text())

