from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from ai_builder.api import create_app
from ai_builder.evidence import ArtifactStore, evidence_support
from ai_builder.pricing import Pricing, TokenUsage


def test_token_subsets_are_not_double_billed():
    tariff = Pricing(provider="openai", model="fixture", effective_date="2026-11-15",
                     source_url="https://example.com/fixture", currency="USD", input_per_million=10,
                     cached_input_per_million=1, output_per_million=50, per_credit=0, input_context_limit=272000)
    usage = TokenUsage(input_tokens=1000, output_tokens=200, cached_input_tokens=500, reasoning_tokens=100)
    assert tariff.token_cost(usage) == Decimal("0.0155")
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=1, output_tokens=1, cached_input_tokens=2, reasoning_tokens=0)


def test_artifact_integrity_and_no_path_traversal(tmp_path):
    artifacts = ArtifactStore(tmp_path / "evidence")
    ref = artifacts.put(b"original")
    manifest = artifacts.manifest([ref], {"revision": "1"})
    assert artifacts.verify_manifest(manifest)["entries"][0]["size"] == 8
    with pytest.raises(ValueError):
        artifacts.get("../../secrets.env")
    (artifacts.root / ref).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        artifacts.verify_manifest(manifest)


def test_parallel_snapshot_publication_is_atomic(tmp_path):
    artifacts = ArtifactStore(tmp_path)
    with ThreadPoolExecutor(4) as pool:
        refs = list(pool.map(artifacts.put, [b"payload" * 10000] * 8))
    assert len(set(refs)) == 1
    assert artifacts.get(refs[0]) == b"payload" * 10000


def test_excerpt_presence_is_required_but_not_semantic_validation():
    assert evidence_support("Price:\n$10", "Price: $10 per month")
    assert not evidence_support("$20", "Price: $10 per month")
    assert not evidence_support("", "anything")


def test_liveness_needs_no_provider_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config_path = Path(__file__).parents[1] / "config" / "baseline.json"
    with TestClient(create_app(tmp_path, config_path, database_url="")) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 503
