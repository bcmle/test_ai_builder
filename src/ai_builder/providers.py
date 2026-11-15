"""Metered HTTP adapters. Each physical attempt is reserved before transmission.

These adapters never share model conversations and never invoke hidden SDK retries.
They are transport building blocks, not the research orchestration worker.
"""

import asyncio
from decimal import Decimal
import ipaddress
import inspect
import json
import random
import socket
import time
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from .pricing import Pricing, TokenUsage
from .store import Store, uid
from .pg_ledger import digest


class ProviderFailure(Exception):
    def __init__(self, code, retryable=False, *, candidate=None, diagnostics=None):
        self.code, self.retryable = code, retryable
        # Private correction inputs, never included in the exception message/public code.
        self.candidate, self.diagnostics = candidate, diagnostics or []
        super().__init__(code)  # Never include provider response bodies/keys in errors.


def validation_diagnostics(error):
    if isinstance(error, ValidationError):
        return [{'code': item['type'], 'path': list(item['loc']), 'message': item['msg'][:600]}
                for item in error.errors(include_url=False, include_context=False, include_input=False)[:30]]
    return [{'code': 'semantic_validation', 'path': [], 'message': str(error)[:600]}]


async def public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only credential-free public HTTP(S) URLs are allowed")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Unsupported source port")
    if parsed.hostname.lower() == "localhost":
        raise ValueError("Local source is forbidden")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443, 0, socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Source resolves to a non-public address")
    return url


class MeteredHTTP:
    def __init__(self, store: Store, run_id: str, agent: str, pricing: Pricing, purpose="live",
                 client: httpx.AsyncClient | None = None, sleep=asyncio.sleep, deadline=None, operation_key=None):
        self.store, self.run_id, self.agent = store, run_id, agent
        self.pricing, self.purpose = pricing, purpose
        self.pricing_id = None
        self.operation_key = operation_key
        self.client = client
        self.sleep = sleep
        self.deadline = deadline

    async def accounting(self, method, *args, **kwargs):
        result = getattr(self.store, method)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def post(self, endpoint, key, payload, upper, usage_parser):
        durable = hasattr(self.store, 'begin_operation')
        if durable:
            if not self.operation_key:
                raise ValueError('Durable operation key required for PostgreSQL transport')
            packet_hash = digest({'endpoint': endpoint, 'payload': payload, 'agent': self.agent,
                                  'purpose': self.purpose, 'pricing': self.pricing.model_dump(mode='json')})
            cached = await self.store.begin_operation(self.operation_key, packet_hash)
            if cached is not None:
                return cached
        self.pricing_id = await self.accounting('register_pricing', self.pricing.model_dump(mode='json'))
        result = await self._post(endpoint, key, payload, upper, usage_parser)
        if durable:
            await self.store.finish_operation(self.operation_key, result)
        return result

    async def _post(self, endpoint, key, payload, upper, usage_parser):
        provider = self.pricing.provider
        base = {"openai": "https://api.openai.com/v1/", "tavily": "https://api.tavily.com/"}[provider]
        if (provider, endpoint) not in {("openai", "responses"), ("tavily", "search"), ("tavily", "extract")}:
            raise ValueError("Endpoint not allowed")
        if not key:
            raise ProviderFailure("missing_credentials")
        logical = uid()
        for attempt in range(3):
            response_limit = 600 if provider == 'openai' and payload.get('reasoning', {}).get('effort') == 'high' else 120
            timeout = min(response_limit, self.deadline - time.monotonic()) if self.deadline else response_limit
            if timeout <= 0:
                raise ProviderFailure("deadline_exhausted")
            request_id = uid()
            reserved = await self.accounting('reserve', run_id=self.run_id, request_id=request_id, logical_request_id=logical,
                               provider=provider, model=self.pricing.model, endpoint=endpoint, agent=self.agent,
                               purpose=self.purpose, pricing_id=self.pricing_id, upper=upper)
            if not reserved:
                raise ProviderFailure('duplicate_physical_attempt')
            start = time.monotonic()
            client = self.client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
            retry_after = None
            try:
                response = await client.post(base + endpoint, headers={"Authorization": "Bearer " + key},
                                             json=payload, timeout=timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                await self.accounting('release', request_id, "Connection failed before HTTP request transmission")
                failure = ProviderFailure("connection_failed", True)
            except httpx.TransportError:
                await self.accounting('mark_unknown', request_id, "transport_error")
                failure = ProviderFailure("transport_unknown", True)
            except BaseException:
                await self.accounting('mark_unknown', request_id, "interrupted")
                raise
            else:
                if response.status_code >= 300:
                    # Without documented usage/no-billing evidence, preserve the reserve.
                    await self.accounting('mark_unknown', request_id, f"http_{response.status_code}")
                    error_code = None
                    try:
                        error = response.json().get("error", {})
                        if isinstance(error, dict):
                            error_code = error.get("code") or error.get("type")
                    except (ValueError, AttributeError):
                        pass
                    exhausted = error_code in {"insufficient_quota", "credit_balance_exhausted", "quota_exceeded"}
                    retry = (response.status_code == 429 and not exhausted) or response.status_code >= 500
                    failure = ProviderFailure("quota_exhausted" if exhausted else f"http_{response.status_code}", retry)
                    if exhausted or response.status_code in (401, 403):
                        await self.accounting('pause_run', self.run_id, failure.code)
                    try:
                        retry_after = float(response.headers.get("retry-after", ""))
                        if not 0 <= retry_after < float("inf"):
                            retry_after = None
                    except ValueError:
                        pass
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        await self.accounting('mark_unknown', request_id, "invalid_provider_json")
                        raise ProviderFailure("invalid_provider_json") from None
                    try:
                        amount, metrics = usage_parser(data)
                        metrics.update(duration_ms=int((time.monotonic() - start) * 1000), status=data.get("status", "completed"),
                                       provider_request_id=response.headers.get("x-request-id"))
                        await self.accounting('settle', request_id, amount, metrics)
                    except (KeyError, TypeError, ValueError, ValidationError):
                        await self.accounting('mark_unknown', request_id, "usage_unavailable")
                        raise ProviderFailure("usage_unavailable") from None
                    return data
            finally:
                if self.client is None:
                    await client.aclose()
            if not failure.retryable or attempt == 2:
                raise failure
            delay = retry_after if retry_after is not None else min(30, 2 * 2 ** attempt) + random.random()
            # An excessive Retry-After is an explicit pause condition, not an early retry.
            if delay > 60 or (self.deadline and time.monotonic() + delay >= self.deadline):
                await self.accounting('pause_run', self.run_id, "retry_after_exceeds_deadline")
                raise ProviderFailure("retry_after_exceeds_deadline")
            await self.sleep(delay)
        raise AssertionError("Unreachable retry state")


class OpenAIAdapter:
    def __init__(self, transport: MeteredHTTP, key: str, reasoning_effort="low"):
        if transport.pricing.provider != "openai":
            raise ValueError("OpenAI tariff required")
        if reasoning_effort not in {'low', 'medium', 'high'}:
            raise ValueError('Unsupported reasoning effort')
        self.reasoning_effort = reasoning_effort
        self.transport, self.key = transport, key

    async def generate(self, system: str, packet: dict, output_type: type[BaseModel], max_output_tokens=4096):
        if not 1 <= max_output_tokens <= 16384:
            raise ValueError("Invalid output token budget")
        schema = output_type.model_json_schema()
        # OpenAI strict structured output requires every object property to be required.
        def strict(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    node["additionalProperties"] = False
                    node["required"] = list(node.get("properties", {}))
                node.pop("default", None)
                for value in node.values():
                    strict(value)
            elif isinstance(node, list):
                for value in node:
                    strict(value)
        strict(schema)
        payload = {"model": self.transport.pricing.model, "store": False,
                   "instructions": system,
                   "input": [{"role": "user", "content": json.dumps(packet, ensure_ascii=False)}],
                   "reasoning": {"effort": self.reasoning_effort}, "max_output_tokens": max_output_tokens,
                   "text": {"format": {"type": "json_schema", "name": output_type.__name__, "strict": True, "schema": schema}}}
        if self.transport.pricing.model == 'gpt-6-astra':
            # The current tariff covers ordinary input/output only. Explicit
            # mode without breakpoints avoids unaccounted automatic cache writes.
            payload['prompt_cache_options'] = {'mode': 'explicit'}
            payload['service_tier'] = 'default'
        # UTF-8 byte bound plus a conservative framing allowance; reject long-context tariff crossover.
        input_upper = len(json.dumps(payload, ensure_ascii=False).encode()) + 4096
        tariff = self.transport.pricing
        if input_upper > tariff.input_context_limit:
            raise ProviderFailure("input_exceeds_pricing_scope")
        upper = tariff.token_cost(TokenUsage(input_tokens=input_upper, output_tokens=max_output_tokens,
                                            cached_input_tokens=0, reasoning_tokens=0))
        def usage(data):
            raw = data["usage"]
            tokens = TokenUsage(input_tokens=raw["input_tokens"], output_tokens=raw["output_tokens"],
                                cached_input_tokens=raw["input_tokens_details"]["cached_tokens"],
                                reasoning_tokens=raw["output_tokens_details"]["reasoning_tokens"])
            return tariff.token_cost(tokens), tokens.model_dump()
        data = await self.transport.post("responses", self.key, payload, upper, usage)
        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "refusal":
                    raise ProviderFailure("model_refusal")
                if content.get("type") == "output_text":
                    chunks.append(content["text"])
        candidate = ''.join(chunks)
        if len(candidate) > 131072:
            raise ProviderFailure('model_output_too_large')
        if data.get('status') != 'completed':
            raise ProviderFailure('incomplete_model_response', candidate=candidate,
                                  diagnostics=[{'code': 'incomplete_model_response', 'path': [],
                                                'message': 'Return a concise complete result within the output limit.'}])
        try:
            return output_type.model_validate_json(candidate)
        except ValidationError as error:
            raise ProviderFailure('invalid_structured_output', candidate=candidate,
                                  diagnostics=validation_diagnostics(error)) from None


class TavilyAdapter:
    def __init__(self, transport: MeteredHTTP, key: str, url_validator=public_url):
        if transport.pricing.provider != "tavily":
            raise ValueError("Tavily tariff required")
        self.transport, self.key, self.url_validator = transport, key, url_validator

    def usage(self, data):
        credits = Decimal(str(data["usage"]["credits"]))
        return self.transport.pricing.credit_cost(credits), {"credits": str(credits)}

    async def search(self, query: str, advanced=False, domains=None, max_results=5):
        if not query.strip() or len(query) > 2000:
            raise ValueError("Search query must be nonempty and bounded")
        if type(max_results) is not int or not 1 <= max_results <= 10:
            raise ValueError("Search result limit must be between one and ten")
        payload = {"query": query, "search_depth": "advanced" if advanced else "basic", "max_results": max_results,
                   "include_usage": True, "include_answer": False, "include_raw_content": False,
                   "auto_parameters": False, "include_domains": domains or []}
        return await self.transport.post("search", self.key, payload,
                    self.transport.pricing.credit_cost(Decimal(2 if advanced else 1)), self.usage)

    async def extract(self, url: str):
        await self.url_validator(url)
        data = await self.transport.post("extract", self.key,
                    {"urls": [url], "extract_depth": "basic", "format": "markdown", "include_usage": True},
                    self.transport.pricing.credit_cost(Decimal(1)), self.usage)
        for item in data.get("results", []):
            await self.url_validator(item["url"])
            if len(item.get("raw_content", "").encode()) > 5 * 1024 * 1024:
                raise ProviderFailure("document_too_large")
        return data
