"""Bounded document parsing and private, immutable acquisition snapshots.

Acquisition policy is the fetcher's responsibility. Parsing proves neither a
claim's truth nor its applicability. Original bytes never become public assets.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

from .evidence import ArtifactStore

if TYPE_CHECKING:
    from .fetch_transport import SourceResponse


@dataclass(frozen=True)
class ParserLimits:
    max_bytes: int = 5_000_000
    max_pages: int = 100
    max_text_chars: int = 500_000
    max_pdf_stream_bytes: int = 10_000_000
    wall_seconds: float = 20.0
    cpu_seconds: int = 20
    memory_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        caps = {"max_bytes": 5_000_000, "max_pages": 100,
                "max_text_chars": 500_000, "max_pdf_stream_bytes": 10_000_000,
                "cpu_seconds": 20, "memory_bytes": 512 * 1024 * 1024}
        for name, cap in caps.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError(f"Invalid parser limit: {name}")
        if not math.isfinite(self.wall_seconds) or not 0 < self.wall_seconds <= 20:
            raise ValueError("Invalid parser wall deadline")


@dataclass(frozen=True)
class ParsedDocument:
    status: str
    body_text: str = ""
    title: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    extraction_method: str | None = None
    parser_version: str | None = None
    error: str | None = None
    page_count: int | None = None
    page_spans: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    publication_metadata: tuple[str, ...] = ()
    original_hash: str | None = None
    parser_limits: dict | None = None


@dataclass(frozen=True)
class StoredSourceSnapshot:
    snapshot_id: str
    manifest_ref: str
    original_ref: str | None
    text_ref: str | None
    parser_ref: str
    status: str


def _failure(code: str, *, method=None, version=None, unsupported=False):
    return ParsedDocument(status="unsupported" if unsupported else "failed",
                          extraction_method=method, parser_version=version, error=code)


def parse_document(response: SourceResponse, limits: ParserLimits | None = None) -> ParsedDocument:
    """Parse only policy-checked fetched bytes, in a killable resource-limited child.

    Limits are inclusive. Exceeding a bound fails the document; no truncated text
    is presented as a successful read. Child environment contains no API secrets.
    """
    limits = limits or ParserLimits()
    if not isinstance(response.body, bytes):
        raise TypeError("Source body must be bytes")
    result = _parse_document(response, limits)
    return replace(result, original_hash=hashlib.sha256(response.body).hexdigest(),
                   parser_limits=asdict(limits))


def _parse_document(response: SourceResponse, limits: ParserLimits) -> ParsedDocument:
    if response.policy_status != "validated" or response.acquisition_method != "controlled_http":
        return _failure("unvalidated_acquisition")
    if response.status_code != 200:
        return _failure("http_status")
    if not response.body:
        return _failure("empty_document", unsupported=True)
    if len(response.body) > limits.max_bytes:
        return _failure("document_byte_limit")
    mime = response.content_type.split(";", 1)[0].strip().lower()
    if mime not in {"text/html", "application/xhtml+xml", "application/pdf"}:
        return _failure("unsupported_mime", unsupported=True)
    # -I ignores PYTHONPATH, site customization and the working directory. The
    # only explicit path is this installed package's trusted parent directory.
    code = ("import sys;sys.path.insert(0,sys.argv[1]);"
            "from ai_builder.source_documents import _parser_child;_parser_child()")
    try:
        child = subprocess.run(
            [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1]),
             mime, json.dumps(asdict(limits))],
            input=response.body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=limits.wall_seconds, check=False, env={"LANG": "C.UTF-8"},
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return _failure("parser_timeout")
    if child.returncode != 0:
        return _failure("parser_resource_or_process_failure")
    try:
        result = json.loads(child.stdout)
        for name in ("page_spans", "warnings", "publication_metadata"):
            result[name] = tuple(result.get(name, ()))
        document = ParsedDocument(**result)
    except (ValueError, TypeError):
        return _failure("parser_invalid_output")
    if len(document.body_text) > limits.max_text_chars:
        return _failure("text_character_limit")
    return document


def _plain(value, limit=1000):
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:limit] or None


def _publication_date(values):
    normalized = set()
    for value in values:
        try:
            parsed = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00"))
            normalized.add(parsed.isoformat())
        except ValueError:
            continue
    return next(iter(normalized)) if len(normalized) == 1 else None


def _html(body: bytes, limits: ParserLimits) -> ParsedDocument:
    import trafilatura
    from lxml import html

    method, version = "trafilatura", trafilatura.__version__
    tree = html.fromstring(body, parser=html.HTMLParser(no_network=True))
    for meta in tree.xpath("//meta[@content]"):
        name = (meta.get("name") or "").lower()
        if name in {"robots", "aibusinessbuilder"}:
            directives = re.sub(r"\s*:\s*", ":", meta.get("content", "").lower())
            if any(item in {"noai", "noarchive", "nosnippet", "max-snippet:0"}
                   for item in re.split(r"[,;\s]+", directives)):
                return _failure("source_policy_denied", method=method, version=version, unsupported=True)
    # Read only explicit publication metadata. Copyright dates, URL dates,
    # modification dates and parser guesses do not become publication dates.
    values = []
    for meta in tree.xpath("//meta[@content]"):
        name = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if name in {"article:published_time", "datepublished", "publication_date"}:
            value = _plain(meta.get("content"), 100)
            if value and len(values) < 20:
                values.append(value)
    titles = tree.xpath("//title/text()")
    publishers = tree.xpath('//meta[translate(@property,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="og:site_name"]/@content')
    title = _plain(titles[0]) if titles else None
    publisher = _plain(publishers[0]) if publishers else None
    for node in tree.xpath('//script|//style|//noscript|//template|//iframe|//object|//form|//*[@hidden]|//*[@aria-hidden="true"]'):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    text = trafilatura.extract(tree, output_format="txt", include_comments=False,
                               include_tables=True, include_links=False,
                               include_images=False, with_metadata=False)
    if not text or not text.strip():
        return _failure("no_extractable_text", method=method, version=version, unsupported=True)
    text = text.strip()
    if len(text) > limits.max_text_chars:
        return _failure("text_character_limit", method=method, version=version)
    published_at = _publication_date(values)
    warnings = ("publication_metadata_unresolved",) if values and published_at is None else ()
    return ParsedDocument("succeeded", text, title, publisher, published_at,
                          method, version, warnings=warnings,
                          publication_metadata=tuple(values))


def _pdf(body: bytes, limits: ParserLimits) -> ParsedDocument:
    import pypdf

    method, version = "pypdf", pypdf.__version__
    if not body.startswith(b"%PDF-"):
        return _failure("invalid_pdf", method=method, version=version)
    reader = pypdf.PdfReader(io.BytesIO(body), strict=True)
    if reader.is_encrypted:
        return _failure("encrypted_pdf", method=method, version=version, unsupported=True)
    count = len(reader.pages)
    if count > limits.max_pages:
        return _failure("pdf_page_limit", method=method, version=version)
    chunks, spans, total, empty_pages = [], [], 0, []
    for index, page in enumerate(reader.pages, 1):
        content = page.get_contents()
        if content is not None and len(content.get_data()) > limits.max_pdf_stream_bytes:
            return _failure("pdf_stream_limit", method=method, version=version)
        text = page.extract_text().strip()
        if not text:
            empty_pages.append(index)
        prefix = "\n\n" if chunks else ""
        start = total + len(prefix)
        total = start + len(text)
        if total > limits.max_text_chars:
            return _failure("text_character_limit", method=method, version=version)
        chunks.append(prefix + text)
        spans.append({"page": index, "start": start, "end": total})
    text = "".join(chunks)
    if not text.strip():
        return _failure("pdf_without_text_ocr_unsupported", method=method, version=version, unsupported=True)
    # PDF CreationDate is file creation, not publication; Author is not publisher.
    metadata = reader.metadata
    title = _plain(metadata.title) if metadata else None
    warnings = ("pages_without_text:" + ",".join(map(str, empty_pages)),) if empty_pages else ()
    return ParsedDocument("succeeded", text, title=title, extraction_method=method,
                          parser_version=version, page_count=count,
                          page_spans=tuple(spans), warnings=warnings)


def _parser_child():
    """Internal subprocess entry. Limits precede importing document libraries."""
    import resource

    mime = sys.argv[2]
    limits = ParserLimits(**json.loads(sys.argv[3]))
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        body = sys.stdin.buffer.read(limits.max_bytes + 1)
        if len(body) > limits.max_bytes:
            result = _failure("document_byte_limit")
        else:
            result = _pdf(body, limits) if mime == "application/pdf" else _html(body, limits)
    except MemoryError:
        result = _failure("parser_memory_limit")
    except Exception:
        # Parser exception text can contain source content; keep it private and
        # deterministic instead of leaking it into general process logs.
        result = _failure("parser_invalid_document")
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=True))


class SourceDocuments:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def save(self, response: SourceResponse, parsed: ParsedDocument, *, source_id: str,
             run_id: str) -> StoredSourceSnapshot:
        """New snapshot identity for each read; identical bytes share only blobs."""
        if not source_id or not run_id:
            raise ValueError("Source and run identities are required")
        if response.fetched_at.tzinfo is None or response.fetched_at.utcoffset() is None:
            raise ValueError("Acquisition time must be timezone aware")
        if len(response.body) > ParserLimits().max_bytes:
            raise ValueError("Oversize source must not enter artifact storage")
        if parsed.original_hash != hashlib.sha256(response.body).hexdigest():
            raise ValueError("Parse result belongs to different source bytes")
        if parsed.status == "succeeded" and (not parsed.body_text.strip() or parsed.error):
            raise ValueError("Invalid successful parse")
        if parsed.status == "succeeded" and (response.policy_status != "validated" or
                                               response.acquisition_method != "controlled_http"):
            raise ValueError("Unvalidated acquisition cannot be a successful document")
        denied = parsed.error == "source_policy_denied"
        original = None if denied else self.artifacts.put(response.body)
        text = self.artifacts.put(parsed.body_text.encode("utf-8")) if parsed.status == "succeeded" else None
        parser = self.artifacts.put(json.dumps(asdict(parsed), sort_keys=True, separators=(",", ":")).encode())
        snapshot_id = str(uuid4())
        metadata = {
            "kind": "source_snapshot", "schema_version": "1.2", "snapshot_id": snapshot_id,
            "source_id": source_id, "run_id": run_id,
            "requested_url": response.requested_url, "final_url": response.final_url,
            "fetched_at": response.fetched_at.astimezone(timezone.utc).isoformat(),
            "mime": response.content_type, "http_status": response.status_code,
            "redirect_chain": list(response.redirect_chain), "peer_ips": list(response.peer_ips),
            "policy_status": "denied" if denied else response.policy_status,
            "provenance": response.acquisition_method,
            "original_ref": original, "body_ref": text, "parser_ref": parser,
            "raw_artifact_ref": original, "raw_hash": parsed.original_hash,
            "extraction_warnings": list(parsed.warnings), "parser_limits": parsed.parser_limits,
            "content_hash": hashlib.sha256(parsed.body_text.encode()).hexdigest() if text else None,
            "fetch_status": parsed.status, "error": parsed.error,
            "title": parsed.title, "publisher": parsed.publisher, "published_at": parsed.published_at,
            "source_kind": "unknown", "event_date": None, "applicability": "unverified",
            "extraction_method": parsed.extraction_method, "parser_version": parsed.parser_version,
        }
        refs = [parser] + ([original] if original else []) + ([text] if text else [])
        manifest = self.artifacts.manifest(refs, metadata)
        return StoredSourceSnapshot(snapshot_id, manifest, original, text, parser, parsed.status)
