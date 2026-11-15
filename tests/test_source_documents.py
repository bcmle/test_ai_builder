from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import sys

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ai_builder.evidence import ArtifactStore
from ai_builder.source_documents import (ParserLimits, SourceDocuments, _html, _pdf, _failure,
                                         parse_document as production_parse_document)


def parse_document(response, limits=None):
    """Darwin fixtures test pure extraction; Linux tests the production subprocess.

    macOS cannot install the required RLIMIT_AS. There is deliberately no such
    fallback in production. Process resource protection is exercised on Linux CI.
    """
    limits = limits or ParserLimits()
    result = production_parse_document(response, limits)
    if sys.platform == "darwin" and result.error == "parser_resource_or_process_failure":
        try:
            result = _pdf(response.body, limits) if response.content_type == "application/pdf" else _html(response.body, limits)
        except Exception:
            result = _failure("parser_invalid_document")
        result = replace(result, original_hash=hashlib.sha256(response.body).hexdigest(),
                         parser_limits=asdict(limits))
    return result


@dataclass(frozen=True)
class ResponseFixture:
    body: bytes
    content_type: str = "text/html"
    requested_url: str = "https://example.com/pricing"
    final_url: str = "https://example.com/pricing"
    fetched_at: datetime = datetime(2026, 11, 15, tzinfo=timezone.utc)
    status_code: int = 200
    redirect_chain: tuple[str, ...] = ()
    peer_ips: tuple[str, ...] = ("93.184.216.34",)
    policy_status: str = "validated"
    acquisition_method: str = "controlled_http"


ARTICLE = b"""<!doctype html><html><head><meta charset="utf-8">
<title>Example subscription plans</title><meta property="og:site_name" content="Example Research">
</head><body><nav>Navigation boilerplate</nav><main><article>
<h1>Subscription plans for small businesses</h1>
<p>Our service helps teams manage recurring invoices and track their customer
payments. Customers can choose a monthly subscription and cancel at the end of
the billing period. Prices below apply to the listed plan and exclude tax.</p>
<table><tr><th>Plan</th><th>Monthly price</th></tr>
<tr><td>Starter</td><td>$19</td></tr><tr><td>Team</td><td>$49</td></tr></table>
<p>The Team subscription includes shared reporting for five employees.
Additional employees cost $4 per month. Contact our support team to discuss
larger deployments and confirm the applicable commercial terms.</p>
<script>STEAL_SECRET();</script><div hidden>Hidden offer for $9999</div>
</article></main></body></html>"""


def pdf_document(texts, *, compressed=False, encrypted=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        if text:
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 50 700 Td (" + text.encode("ascii") + b") Tj ET")
            if compressed:
                stream = stream.flate_encode()
            page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": "Synthetic annual report", "/CreationDate": "D:20260101000000Z",
                         "/Author": "File author, not publisher"})
    if encrypted:
        writer.encrypt("fixture-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_html_article_and_pricing_table_are_plain_text():
    document = parse_document(ResponseFixture(ARTICLE))
    assert document.status == "succeeded", document.error
    assert document.title == "Example subscription plans"
    assert document.publisher == "Example Research"
    for text in ("Starter", "$19", "Team", "$49", "Additional employees cost $4"):
        assert text in document.body_text
    for text in ("STEAL_SECRET", "Hidden offer", "<script>"):
        assert text not in document.body_text
    assert document.extraction_method == "trafilatura"
    assert document.parser_version == "2.3.1"
    assert document.published_at is None


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS RLIMIT_AS is unavailable; Linux CI enforces hard memory limits")
def test_production_parser_runs_with_hard_resource_limits():
    assert production_parse_document(ResponseFixture(ARTICLE)).status == "succeeded"
    limited = production_parse_document(ResponseFixture(ARTICLE), ParserLimits(memory_bytes=1024))
    assert limited.status == "failed"
    assert not limited.body_text


@pytest.mark.parametrize("directive,allowed", [("noarchive", False), ("noai", False),
                                               ("nosnippet", False), ("max-snippet:0", False),
                                               ("noindex", True)])
def test_explicit_html_snapshot_restrictions(directive, allowed):
    body = ARTICLE.replace(b"</head>", f'<meta name="robots" content="{directive}"></head>'.encode())
    document = parse_document(ResponseFixture(body))
    assert (document.status == "succeeded") == allowed
    if not allowed:
        assert document.error == "source_policy_denied"


@pytest.mark.parametrize("metadata,expected", [
    (b'<meta property="article:published_time" content="2026-11-01T12:00:00Z">', "2026-11-01T12:00:00+00:00"),
    (b'<meta itemprop="datePublished" content="2026-11-01">', "2026-11-01"),
    (b'<meta property="article:modified_time" content="2026-11-01">', None),
    (b'<meta property="article:published_time" content="2026-11-01"><meta itemprop="datePublished" content="2026-10-01">', None),
    (b'<meta property="article:published_time" content="unknown">', None),
])
def test_only_explicit_unambiguous_publication_date(metadata, expected):
    response = ResponseFixture(ARTICLE.replace(b"</head>", metadata + b"</head>"),
                               final_url="https://example.com/2026/11/15/story")
    document = parse_document(response)
    assert document.status == "succeeded"
    assert document.published_at == expected


def test_pdf_report_preserves_text_offsets_and_does_not_invent_publication_date():
    response = ResponseFixture(pdf_document(["Annual recurring revenue: USD 123 million.",
                                             "Subscription customers: 45000."]), "application/pdf")
    first, second = parse_document(response), parse_document(response)
    assert first.status == "succeeded", first.error
    assert first == second
    assert first.page_count == 2
    assert first.title == "Synthetic annual report"
    assert first.published_at is None
    assert first.publisher is None
    assert first.parser_version == "7.1.0"
    for span, text in zip(first.page_spans, ["Annual recurring revenue: USD 123 million.",
                                          "Subscription customers: 45000."]):
        assert first.body_text[span["start"]:span["end"]] == text


@pytest.mark.parametrize("texts,encrypted,error", [
    ([""], False, "pdf_without_text_ocr_unsupported"),
    (["Secret"], True, "encrypted_pdf"),
])
def test_pdf_without_accessible_text_is_unsupported(texts, encrypted, error):
    document = parse_document(ResponseFixture(pdf_document(texts, encrypted=encrypted), "application/pdf"))
    assert document.status == "unsupported"
    assert document.error == error
    assert document.body_text == ""


def test_mixed_pdf_records_unread_pages():
    document = parse_document(ResponseFixture(pdf_document(["Revenue statement.", ""]), "application/pdf"))
    assert document.status == "succeeded"
    assert document.warnings == ("pages_without_text:2",)


@pytest.mark.parametrize("body,mime,limits,error", [
    (ARTICLE, "text/html", ParserLimits(max_bytes=10), "document_byte_limit"),
    (ARTICLE, "text/html", ParserLimits(max_text_chars=10), "text_character_limit"),
    (pdf_document(["one", "two"]), "application/pdf", ParserLimits(max_pages=1), "pdf_page_limit"),
    (pdf_document(["A" * 1000], compressed=True), "application/pdf", ParserLimits(max_pdf_stream_bytes=100), "pdf_stream_limit"),
    (ARTICLE, "text/html", ParserLimits(wall_seconds=0.001), "parser_timeout"),
])
def test_limits_fail_closed_without_partial_success(body, mime, limits, error):
    document = parse_document(ResponseFixture(body, mime), limits)
    assert document.status == "failed"
    assert document.error == error
    assert document.body_text == ""


@pytest.mark.parametrize("response,error", [
    (ResponseFixture(ARTICLE, policy_status="unknown"), "unvalidated_acquisition"),
    (ResponseFixture(ARTICLE, acquisition_method="external_extract"), "unvalidated_acquisition"),
    (ResponseFixture(ARTICLE, status_code=403), "http_status"),
    (ResponseFixture(b"", "text/html"), "empty_document"),
    (ResponseFixture(b"snippet", "text/plain"), "unsupported_mime"),
    (ResponseFixture(b"Not a PDF", "application/pdf"), "invalid_pdf"),
    (ResponseFixture(b"%PDF-1.7\nmalformed", "application/pdf"), "parser_invalid_document"),
])
def test_acquisition_and_format_failures_are_not_evidence(response, error):
    document = parse_document(response)
    assert document.status != "succeeded"
    assert document.error == error
    assert not document.body_text


def test_snapshots_keep_original_and_text_hashes_and_create_new_read_identity(tmp_path):
    artifacts = ArtifactStore(tmp_path / "evidence")
    store = SourceDocuments(artifacts)
    response = ResponseFixture(ARTICLE)
    parsed = parse_document(response)
    first = store.save(response, parsed, source_id="source-1", run_id="run-1")
    second = store.save(response, parsed, source_id="source-1", run_id="run-2")
    assert first.snapshot_id != second.snapshot_id
    assert first.manifest_ref != second.manifest_ref
    assert first.original_ref == second.original_ref == hashlib.sha256(ARTICLE).hexdigest()
    assert first.text_ref == second.text_ref
    assert artifacts.get(first.original_ref) == ARTICLE
    assert artifacts.get(first.text_ref).decode() == parsed.body_text
    metadata = artifacts.verify_manifest(first.manifest_ref)["metadata"]
    assert metadata["source_kind"] == "unknown"
    assert metadata["event_date"] is None
    assert metadata["applicability"] == "unverified"
    assert metadata["published_at"] is None
    assert metadata["requested_url"] == response.requested_url
    assert metadata["content_hash"] == first.text_ref
    assert metadata["raw_hash"] == metadata["raw_artifact_ref"] == first.original_ref
    assert metadata["extraction_warnings"] == []
    assert metadata["parser_limits"]["max_pages"] == 100
    parser = json.loads(artifacts.get(first.parser_ref))
    assert parser["parser_limits"]["memory_bytes"] == 512 * 1024 * 1024
    assert parser["parser_limits"]["max_pages"] == 100
    assert parser["original_hash"] == first.original_ref
    (artifacts.root / first.original_ref).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        artifacts.verify_manifest(first.manifest_ref)


def test_failed_extraction_keeps_raw_document_without_evidence_text(tmp_path):
    artifacts = ArtifactStore(tmp_path)
    response = ResponseFixture(pdf_document([""]), "application/pdf")
    parsed = parse_document(response)
    snapshot = SourceDocuments(artifacts).save(response, parsed, source_id="source", run_id="run")
    metadata = artifacts.verify_manifest(snapshot.manifest_ref)["metadata"]
    assert snapshot.text_ref is None
    assert metadata["error"] == "pdf_without_text_ocr_unsupported"
    assert artifacts.get(snapshot.original_ref) == response.body


def test_noarchive_refusal_does_not_store_source_bytes(tmp_path):
    artifacts = ArtifactStore(tmp_path)
    response = ResponseFixture(ARTICLE.replace(b"</head>", b'<meta name="robots" content="noarchive"></head>'))
    parsed = parse_document(response)
    snapshot = SourceDocuments(artifacts).save(response, parsed, source_id="source", run_id="run")
    assert snapshot.original_ref is None
    assert snapshot.text_ref is None
    assert not (artifacts.root / parsed.original_hash).exists()
    metadata = artifacts.verify_manifest(snapshot.manifest_ref)["metadata"]
    assert metadata["policy_status"] == "denied"
    assert metadata["raw_artifact_ref"] is None


def test_parse_result_cannot_be_attached_to_other_bytes(tmp_path):
    response = ResponseFixture(ARTICLE)
    parsed = parse_document(response)
    with pytest.raises(ValueError, match="different source bytes"):
        SourceDocuments(ArtifactStore(tmp_path)).save(replace(response, body=b"other"), parsed,
                                                     source_id="source", run_id="run")


def test_parser_deadline_and_pages_cannot_exceed_approved_limits():
    for kwargs in ({"max_pages": 101}, {"wall_seconds": 21}, {"wall_seconds": float("nan")},
                   {"memory_bytes": 513 * 1024 * 1024}, {"max_bytes": 5_000_001}):
        with pytest.raises(ValueError):
            ParserLimits(**kwargs)
