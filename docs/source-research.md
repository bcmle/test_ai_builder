# Controlled source research

This stage connects a **chosen model** to Tavily Search, controlled reading of open
HTML/text PDFs, and the existing private LangGraph report. It is not the complete
market-discovery catalogue, batch selector, permanent worker or public publication.
Search snippets and generated search answers never become evidence documents.

## Explicit invocation

Apply packaged migrations with the separate migrator role first. Run with a runtime
PostgreSQL role, private writable `AI_BUILDER_DATA`, and environment-only
`OPENAI_API_KEY` / `TAVILY_API_KEY`. Do not paste keys into a command or commit them.
Production parsing requires Linux; the Aeza image provides the supported runtime.

The following command **spends API budget**. Development and CI use fixtures instead.
The example plan needs an owner-selected model and queries before live use. Supply
independently verified tariff files for both providers; no production prices are
invented by the application. The explicit study cap and the configured $25/run cap
both apply to the combined search/model usage.

```sh
python -m ai_builder.research_scope \
  --config config/baseline.json \
  --plan private/selected-source-plan.json \
  --pricing private/verified-openai-pricing.json \
  --tavily-pricing private/verified-tavily-pricing.json \
  --settings config/examples/role-settings.json \
  --study-budget 25 --purpose live
```

Plan structure is illustrated by `config/examples/source-plan.json`. Tariffs use
the `Pricing` contract: provider, model, effective_date, source_url, currency,
input_per_million, cached_input_per_million, output_per_million, per_credit and
input_context_limit. The OpenAI model must match the saved run configuration.
The role-settings example is not an evaluated quality recommendation.

The complete plan, tariffs and role settings are saved before the first request.
The source packet is frozen before analytical agents run. A lease, heartbeat and
persisted active-time deadline protect all stages. No schema changes happen at
CLI startup. Output is a private English draft, never an automatically published card.

## Acquisition and evidence

- One chosen scope has 1–4 explicit queries, at most 10 results per query and at
  most 6 new document reads. These bounds do not imply market coverage.
- Each search result keeps its rank, URL, snippet and disposition. Snippets remain
  discovery metadata. Empty results, denied sources, parser failures and unprocessed
  URLs remain visible gaps; none proves the absence of a business or a fact.
- The HTTP transport validates DNS, the actual connection peer and every redirect.
  It uses no ambient proxy/cookies, rejects non-public and metadata addresses, and
  checks robots/site restrictions before downloading. Unknown policy failures are
  closed; no login, paywall, browser automation or OCR fallback is attempted.
- Downloads share a 20-second deadline, at most 5 redirects, and a 5 MB ceiling
  on both transferred and decoded content. HTML uses Trafilatura; text PDFs use
  pypdf. Extraction runs in a child process with a 20-second wall/CPU ceiling,
  512 MiB address-space limit and a 100-page PDF ceiling. Limits may be reduced.
  Unsupported hard resource limits cause an explicit failure, not an unbounded parser.
- Private manifests bind original bytes, extracted text, parser result, hashes,
  requested/final URL, read time and extraction metadata. Unknown publication
  dates stay unknown. PDF creation dates do not become publication dates.
- Source IDs, snapshot IDs and the graph's legacy document handles are distinct.
  A successfully acquired document is not yet a semantically validated Claim →
  EvidenceLink. Full applicability/manual-review checks remain a subsequent stage.
- Controlled HTTP reads consume the shared document counter without being presented
  as paid provider usage. Tavily Search shares the same monetary ledger as LLM calls.
  Optional Tavily Extract is not enabled by this path.

## Interrupted work and replay

```sh
python -m ai_builder.research_scope --resume-run RUN_ID
```

Resume reloads saved inputs; configuration, tariffs and plans cannot be overridden.
Completed provider operations reuse their persisted receipts. An interrupted source
read or uncertain billed operation remains visible and requires explicit resolution;
it is not silently retried. A paused run is not unpaused by this CLI.

A completed private draft can be re-exported from its saved source packet and
checkpoint without provider credentials or new requests. Artifact integrity is
checked again. Replay is labelled as such and does not claim fresh web research.

Fixture tests cover search → pinned HTTP transport → both parsers → PostgreSQL →
Critic/Verifier → private card, inaccessible sources, shared-budget refusal and
zero-call replay. Linux CI exercises the resource-limited production subprocess.
No live provider acceptance has been inferred from these tests.

API contract reference: [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search).

## Bounded recovery and focused analysis (v1.4)

Existing technical failures can be explicitly selected without re-running the other
collection members or changing frozen inputs:

```sh
python -m ai_builder.collection --resume-collection COLLECTION_ID --retry-failed
# Add --retry-model MODEL_ID to select one failed member.
python -m ai_builder.focused_research --parent-collection COLLECTION_ID --model-id MODEL_ID
```

The focused command uses the packaged Helpdesk plan only for its matching base ID,
creates one linked run in the original study, and reuses that run on subsequent
invocations. Saved provider tariffs and cumulative accounting remain in PostgreSQL.
It publishes a separate scoped snapshot and homepage link; the main twenty-model
collection and its ranking stay separate. `--retry-paused` explicitly resumes a
paused follow-up only when its existing limits allow it and no uncertain external
operation or active owner remains. It does not increase any budget.

If the saved full source packet exceeds the analytical input bound before any
LLM request, `--refine-sources` creates a linked revision using its successfully
downloaded priority primary documents. Their full text, original dates and
snapshot identifiers are retained; supplementary documents stay in the earlier
run. The selection and exclusions are recorded. No Tavily request or document
download is repeated. Subsequent invocations resume the latest revision.

```sh
python -m ai_builder.focused_research --parent-collection COLLECTION_ID --model-id MODEL_ID --refine-sources
```

Role outputs undergo schema and semantic validation before acceptance. An invalid
output receives at most two metered corrections; every saved raw response and
correction is replayable. Refusals, authentication/quota errors and unknown billing
are not disguised as formatting errors. Original invalid receipts remain in the
private audit. Document references and conditional proposed-design references are
distinct; lawful activity, special licensing, recurring revenue and comparable
valuation still require documentary evidence.
