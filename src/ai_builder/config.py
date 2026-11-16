import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, model_serializer, model_validator

from .domain import Contract


class Scenario(Contract):
    team_size: int = Field(gt=0)
    business_budget_usd: Decimal = Field(gt=0)
    salaries_included: bool
    days: Literal[90]
    existing_customers: bool
    jurisdiction: str | None


class Limits(Contract):
    run_cost_usd: Decimal = Field(gt=0)
    active_seconds: int = Field(gt=0)
    provider_requests: int = Field(gt=0)
    search_requests: int = Field(gt=0)
    document_fetches: int = Field(gt=0)
    concurrent_tasks: int = Field(ge=1, le=2)


class Discovery(Contract):
    goal: int = Field(ge=1)
    cap: int = Field(ge=1)
    target: int = Field(ge=1, le=20)

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
    allow_design_basis: bool = False

    @model_serializer(mode='wrap')
    def legacy_compatible(self, handler):
        value = handler(self)
        if not self.allow_design_basis:
            value.pop('allow_design_basis', None)
        return value


class FactorConfig(Contract):
    id: str
    weight: Decimal = Field(ge=0)
    anchors: list[str] = Field(min_length=5, max_length=5)


class Configuration(Contract):
    schema_version: Literal["1.0", "1.1", "1.2"]
    scoring_policy_version: Literal['1.5.2'] | None = None
    language: Literal["en"]
    model: str = Field(min_length=1)
    market: str
    scenario: Scenario
    limits: Limits
    discovery: Discovery
    criteria: list[CriterionConfig]
    factors: list[FactorConfig]

    @model_serializer(mode='wrap')
    def legacy_compatible(self, handler):
        value = handler(self)
        if self.scoring_policy_version is None:
            value.pop('scoring_policy_version', None)
        return value

    @model_validator(mode="after")
    def invariants(self):
        if (self.schema_version == '1.2') != (self.scoring_policy_version == '1.5.2'):
            raise ValueError('Schema 1.2 requires scoring policy 1.5.2; legacy policies cannot be changed')
        if self.market in {"RU", "BY", "AM", "AZ", "KZ", "KG", "MD", "TJ", "TM", "UZ"}:
            raise ValueError("Excluded market")
        if len(self.criteria) != 10 or {c.id for c in self.criteria} != {f"F{i:02}" for i in range(1, 11)}:
            raise ValueError("All ten mandatory filters must be configured exactly once")
        allowed_design = {'F02', 'F05', 'F06', 'F07', 'F08', 'F09'}
        if any(c.allow_design_basis and (self.schema_version not in {'1.1', '1.2'} or c.id not in allowed_design)
               for c in self.criteria):
            raise ValueError('Design basis is limited to the v1.1 operational criteria')
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

