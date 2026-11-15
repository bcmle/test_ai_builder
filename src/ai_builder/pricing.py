"""Versioned, deterministic prices. Provider token subcategories are not extra tokens."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .domain import Contract


class TokenUsage(Contract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def subsets(self):
        if self.cached_input_tokens > self.input_tokens or self.reasoning_tokens > self.output_tokens:
            raise ValueError("Cached/reasoning tokens must be subsets of input/output")
        return self


class Pricing(Contract):
    provider: Literal["openai", "tavily"]
    model: str
    effective_date: str
    source_url: str
    currency: Literal["USD"]
    input_per_million: Decimal = Field(ge=0)
    cached_input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    per_credit: Decimal = Field(ge=0)
    input_context_limit: int = Field(gt=0)

    def token_cost(self, usage: TokenUsage) -> Decimal:
        if self.provider != "openai" or usage.input_tokens > self.input_context_limit:
            raise ValueError("Usage is outside this tariff's model/context scope")
        return ((usage.input_tokens - usage.cached_input_tokens) * self.input_per_million
                + usage.cached_input_tokens * self.cached_input_per_million
                + usage.output_tokens * self.output_per_million) / Decimal(1_000_000)

    def credit_cost(self, credits: Decimal) -> Decimal:
        if self.provider != "tavily" or not credits.is_finite() or credits < 0:
            raise ValueError("Invalid search tariff or credits")
        return credits * self.per_credit

