import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from ai_builder.domain import Contract
from ai_builder.pricing import Pricing
from ai_builder.providers import MeteredHTTP, OpenAIAdapter, TavilyAdapter, ProviderFailure, public_url


def pricing(provider="openai"):
    return Pricing(provider=provider, model="gpt-6-astra" if provider == "openai" else "tavily",
                   effective_date="2026-11-15", source_url="https://example.com/fixture",
                   currency="USD", input_per_million=10, cached_input_per_million=1,
                   output_per_million=50, per_credit="0.008", input_context_limit=272000)


async def no_sleep(seconds):
    pass


class Output(Contract):
    answer: str


def model_result(status="completed", text='{"answer":"ok"}'):
    return {"status": status, "usage": {"input_tokens": 100, "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 5}},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def test_model_has_isolated_input_no_conversation_reuse(ledger):
    store, _, run, _ = ledger
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=model_result())
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAIAdapter(MeteredHTTP(store, run, "critic", pricing(), client=client), "fixture-key")
            assert (await adapter.generate("English only", {"model": "one"}, Output)).answer == "ok"
            assert (await adapter.generate("English only", {"model": "two"}, Output)).answer == "ok"
    asyncio.run(scenario())
    assert all("previous_response_id" not in r and "conversation" not in r and r["store"] is False for r in requests)
    assert len(requests[1]["input"]) == 1 and "one" not in requests[1]["input"][0]["content"]
    assert store.summary(run)["calculated_micro"] == 4000


def test_timeout_reserve_is_held_and_retry_has_new_request_id(ledger):
    store, _, run, _ = ledger
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("fixture")
        return httpx.Response(200, json=model_result())
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAIAdapter(MeteredHTTP(store, run, "critic", pricing(), client=client, sleep=no_sleep), "fixture")
            await adapter.generate("English", {}, Output)
    asyncio.run(scenario())
    rows = store.summary(run)["requests"]
    assert len(rows) == 2 and rows[0]["state"] == "unknown" and rows[1]["state"] == "settled"
    assert rows[0]["logical_request_id"] == rows[1]["logical_request_id"]
    assert rows[0]["request_id"] != rows[1]["request_id"]
    assert store.summary(run)["unknown_micro"] > 0


@pytest.mark.parametrize("status,code", [(401, "invalid_api_key"), (429, "credit_balance_exhausted")])
def test_credentials_or_balance_stop_without_retry(ledger, status, code):
    store, _, run, _ = ledger
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": "Never echo secrets"}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAIAdapter(MeteredHTTP(store, run, "critic", pricing(), client=client, sleep=no_sleep), "fixture")
            with pytest.raises(ProviderFailure) as exc:
                await adapter.generate("English", {}, Output)
            assert "Never echo" not in str(exc.value)
    asyncio.run(scenario())
    assert len(calls) == 1
    with store.connection() as db:
        assert db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0] == "paused"


@pytest.mark.parametrize("status,text,code", [("incomplete", '{}', "incomplete_model_response"),
                                            ("completed", '{"wrong":1}', "invalid_structured_output")])
def test_bad_model_output_is_not_a_business_result(ledger, status, text, code):
    store, _, run, _ = ledger
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=model_result(status,text)))) as client:
            adapter = OpenAIAdapter(MeteredHTTP(store, run, "critic", pricing(), client=client), "fixture")
            with pytest.raises(ProviderFailure, match=code):
                await adapter.generate("English", {}, Output)
    asyncio.run(scenario())
    assert store.summary(run)["calculated_micro"] == 2000  # paid tokens despite invalid result


def test_tavily_returns_sources_without_generated_answer(ledger):
    store, _, run, _ = ledger
    def handler(request):
        body = json.loads(request.content)
        assert body["include_answer"] is False and body["auto_parameters"] is False
        return httpx.Response(200, json={"results": [{"url": "https://example.com", "content": "fixture"}], "usage": {"credits": 1}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = TavilyAdapter(MeteredHTTP(store, run, "discovery", pricing("tavily"), client=client), "fixture")
            return await adapter.search("recurring businesses")
    assert asyncio.run(scenario())["results"][0]["url"] == "https://example.com"
    assert store.summary(run)["calculated_micro"] == 8000


def test_missing_usage_is_not_zero_cost(ledger):
    store, _, run, _ = ledger
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))) as client:
            adapter = TavilyAdapter(MeteredHTTP(store, run, "discovery", pricing("tavily"), client=client), "fixture")
            with pytest.raises(ProviderFailure, match="usage_unavailable"):
                await adapter.search("fixture")
    asyncio.run(scenario())
    assert store.summary(run)["unknown_micro"] == 8000


def test_temporary_failure_has_exactly_three_physical_attempts(ledger):
    store, _, run, _ = ledger
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "unavailable"})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = TavilyAdapter(MeteredHTTP(store, run, "discovery", pricing("tavily"), client=client, sleep=no_sleep), "fixture")
            with pytest.raises(ProviderFailure, match="http_503"):
                await adapter.search("fixture")
    asyncio.run(scenario())
    assert len(calls) == 3
    assert store.summary(run)["unknown_micro"] == 24000


def test_long_retry_after_pauses_instead_of_retrying_early(ledger):
    store, _, run, _ = ledger
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "120"}, json={}))) as client:
            adapter = TavilyAdapter(MeteredHTTP(store, run, "discovery", pricing("tavily"), client=client, sleep=no_sleep), "fixture")
            with pytest.raises(ProviderFailure, match="retry_after_exceeds_deadline"):
                await adapter.search("fixture")
    asyncio.run(scenario())
    assert len(store.summary(run)["requests"]) == 1


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/", "http://user:password@example.com", "http://127.0.0.1/", "http://169.254.169.254/"])
def test_private_sources_rejected(url):
    with pytest.raises(ValueError):
        asyncio.run(public_url(url))


def test_high_reasoning_response_wait_is_bounded_by_run_deadline(ledger):
    import time
    store, _, run, _ = ledger
    observed = []
    def handler(request):
        observed.append(request.extensions['timeout']['read'])
        return httpx.Response(200, json=model_result())
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for effort, remaining in [('high', None), ('high', 45), ('low', None)]:
                transport = MeteredHTTP(store, run, 'research', pricing(), client=client,
                    deadline=time.monotonic() + remaining if remaining else None)
                await OpenAIAdapter(transport, 'fixture', reasoning_effort=effort).generate('English', {}, Output)
    asyncio.run(scenario())
    assert observed[0] == 600 and 0 < observed[1] <= 45 and observed[2] == 120
