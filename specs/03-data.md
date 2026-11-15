# 03. Контракты данных и состояния

Статус: DRAFT v0.2. Это описание будущих схем, не исполняемые JSON Schema.

## Общие правила

ID — непрозрачная строка, уникальная в своей сущности. Время — ISO 8601 UTC; UI отображает часовой пояс явно. Деньги — decimal amount + currency + дата; значения без валюты не принимаются. Поля, перечисленные ниже, обязательны, если не указано nullable. Отсутствующее знание — null/unknown с причиной, никогда выдуманное значение. Неизвестные enum и лишние поля в ответах LLM отклоняются. Все записи содержат schema_version, created_at и run_id, кроме глобальной версии конфигурации.

## Сущности

| Сущность | Основные поля и ограничения |
|---|---|
| Run | id; mode live/replay; config_snapshot; config_hash; prompt_versions; provider/model IDs; launch_context; status; started_at; finished_at nullable; parent_run_id nullable; discovery_complete boolean; counts; budget_usage |
| BusinessModel | id; name; customer; problem; value_proposition; revenue_mechanism; target_market; analog_company_ids; discovery_source_ids; dedupe_key; selection_reason |
| Company | id; name; canonical_domain; operating_markets; model_match_claim_ids |
| Source | id; requested_url; final_url; publisher nullable; title; source_kind primary/secondary; published_at nullable; event_date nullable; fetched_at; fetch_status; snapshot_ref nullable; content_hash nullable; origin_group_id nullable |
| Claim | id; model_id; text; kind fact/calculation/judgment; scope(entity, geography, period nullable, metric nullable); method; evidence_links[]; input_claim_ids[]; formula nullable; confidence low/medium/high; confidence_basis; verification_status; author_stage |
| EvidenceLink | claim_id; source_id; excerpt; locator nullable; relation supports/refutes/context; relevance_note. Excerpt должен находиться в snapshot |
| CriterionResult | model_id; criterion_id; status pass/fail/unknown; claim_ids[]; reason; unresolved_conflict_ids[]; evaluated_at; revision |
| Risk | id; model_id; neutral_claim_id; premise_claim_ids[]; category; impact 1..3; likelihood_note; why_it_matters; proposed_test; critic_order |
| Verification | id; claim_id; status supported/refuted/mixed/insufficient/not_fact_checkable; evidence_links[]; context_checks; explanation; remaining_question nullable |
| Conflict | id; claim_id; alternatives[] (value/text, evidence IDs, dates); material_to_criteria[]; material_to_factors[]; status open/resolved; resolution nullable |
| FactorAssessment | model_id; factor_id; value integer 0..4 nullable; low/high nullable; supporting_claim_ids[]; rationale; missing_evidence[]; version |
| Score | model_id; config_hash; factor_assessment_ids[]; value nullable 0..100; range nullable; status final/provisional/unavailable; calculation_version |
| Card | model_id; revision; eligibility; analysis_status; headline; claim_ids[]; risk_ids[]; score_id nullable; test_plan_claim_ids[]; limitations[]; published_at nullable |
| StageExecution | id; model_id nullable; stage; input_hash; config_hash; prompt_version; state; attempt_count; lease_expires_at nullable; output_ref nullable; error_category nullable |
| AuditEvent | id; execution_id; timestamp; event_type; actor; payload_ref; sanitized_payload; previous_revision nullable |
| ManualReview | id; target_type/id; reviewer; decision; reason; evidence_ids[]; created_at; supersedes_id nullable |

method: primary_source / secondary_source / calculation / model_judgment / human_required. secondary_source — предлагаемое расширение четырёх методов исходного задания для честного представления проверенной вторичной публикации (пункт ревью D-06). source_kind описывает происхождение документа отдельно. Нельзя выдавать вторичную статью за первоисточник. Для подтверждённого факта выбирается метод основного доказательства; все остальные свидетельства остаются в evidence_links. Открытый существенный конфликт или недостаток данных требует human_required. Для прогнозов используется model_judgment, а не метод источника, на котором основана предпосылка.

Для calculation обязательны formula и input_claim_ids. Для model_judgment ссылка может отсутствовать, но нужны предпосылки или явное обозначение допущения. Для human_required — причина и вопрос человеку. Для любого метода дата формирования и дата проверки обязательны; даты источника nullable. Confidence — качественная оценка доказательств, не вероятность истинности. high требует прямой поддержки, совпадения контекста и отсутствия существенного открытого конфликта; medium — документированное косвенное доказательство; low — неполнота/допущение. Самооценка LLM не повышает уверенность автоматически.

## Независимые состояния

- eligibility: pending → pass / fail / needs_review. Любой fail даёт fail; иначе любой unknown даёт needs_review; иначе все pass дают pass. Все критерии должны иметь запись, даже not evaluated представляется unknown с причиной.
- analysis_status: pending → running → complete / needs_review / technical_incomplete. Смена eligibility после найденного факта возможна только новой ревизией.
- stage state: pending → running → succeeded / retry_wait / failed / paused; retry_wait → running; expired lease → pending с событием recovery. succeeded не перезаписывается при resume.
- run status: queued → running → completed / partial / paused / failed. completed означает все этапы доведены до явного конечного решения, включая бизнес needs_review; partial — есть техническая неполнота или discovery < target; paused — ожидается ключ/бюджет/ручное возобновление; failed — нет пригодного результата из-за фатальной ошибки.

Полностью обработанная модель может нуждаться в человеке. «Завершено» не означает «подходит». Счётчики UI взаимно исключаются по приоритету: fail → needs_review (бизнес) → technical_incomplete/pending → ranked. Дополнительно показывать независимую статистику этапов, чтобы не скрыть технические ошибки у отклонённых моделей.

## Инварианты

- В итоговом рейтинге только eligibility=pass, analysis_status=complete и Score.status=final.
- Рейтинг вычисляется для одного run и одного launch_context/config_hash; разные сценарии не смешиваются.
- Никакой итоговый факт в карточке не существует без claim ID и паспорта; заголовки/статические подписи исключаются.
- Все ссылки, claim IDs и evidence IDs существуют в пределах разрешённого run.
- Утверждение не становится supported только из-за URL; нужна проверенная связь с содержанием.
- Ручная правка и повтор этапа создают ревизии. Published snapshot остаётся неизменным до явного publish новой ревизии.

## Дополнение v0.2: накопительное исследование рынков

- MarketStudy: id, market_code, scope_version, directions, source_families, date_window, languages, query_plan, status(active/paused/ready_to_close/closed), owner_decision nullable, cumulative_budget, started_at, closed_at nullable.
- CoverageCell: study_id, direction_id, source_family_id, status(pending/in_progress/covered/blocked), query_ids, discovered_url_ids, read_source_ids, gap_reason nullable.
- DiscoveryCycle: study_id, cycle_id, query_ids, newly_read_unique_pages, newly_discovered_unique_models, novelty_ratio nullable. При знаменателе 0 ratio=null, насыщение не засчитывается.
- CandidateBacklog: global_model_id, study_id, evidence_ids, state(new/selected/processed/deferred/duplicate), run_ids, deferral_reason nullable, duplicate_of nullable.
- Run дополнительно хранит study_id и market_code. ID модели глобален в рамках приложения; факты, роли, оценки и результаты рынка относятся к run/model/market, не перезаписывают результаты другой страны.
- Ранее описанный алгоритм выбора 12 применяется внутри очереди текущего рынка. Переход между рынками не происходит после одного batch автоматически.

Эти контракты расширяют v0.1. Финальные миграции будут проектироваться после ревью, существующей базы приложения пока нет.
