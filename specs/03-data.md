# 03. Контракты данных и состояния

Статус: APPROVED BASELINE v1.3 (пользователь, 15.11.2026).

## Дополнение контрактов v1.4

Новый критерий различает `evidence_ids` документов и типизированный `design_basis`: ссылки на конкретные утверждения замороженного предлагаемого operating design с областью применимости и условиями. Scope ID не является EvidenceLink. Предпосылка не получает источник/дату независимой проверки как установленный внешний факт. Политика допустимого основания задаётся версией конфигурации по 04-validation; legacy-конфигурации сохраняют прежний контракт и хеши, включая JSON schemas, сериализацию и bundle промптов. Новые поля не меняют входы ранее оплаченных операций.

Результат роли считается accepted только после schema и semantic validation. Исходные сырые provider responses и старые receipts неизменяемы; отвергнутый legacy receipt явно отмечается в аудите. Коррекции имеют отдельные детерминированные operation/receipt IDs, номер попытки, хеш исходного разрешённого пакета, ссылку на неверный ответ и безопасные коды/пути ошибок. Восстановление использует сохранённые попытки; счётчик не обнуляется после crash.

Углублённый анализ — новый run/scope с прежним `base_model_id` и проверяемой ссылкой на исходный run/collection. Изменённая юрисдикция, источники, конфигурация и роли явно версионируются. Он сохраняет общий study budget, но не заменяет immutable child identity или membership исходной подборки. Публикация — отдельный снимок с `expected_count=1`, родительской связью и отдельным сценарием; latest-подборка из двадцати остаётся основной. Full editor ревизий по-прежнему отложен.

Ревизия слишком большого пакета дополнительно хранит `derived_from_run_id`, указывающий на первый focused run, и точный manifest повторно используемых source result/snapshot refs. Она получает новые run/scope IDs, сохраняет тот же base model, original parent collection/run и общий study. `source_metadata` сохраняет исходные URL/redirects, даты, publisher и способ получения; повторное использование не изображается как новая загрузка. Исходный frozen-пакет, usage и результаты не перезаписываются. Старые контексты без этого необязательного поля сохраняют сериализацию и хеши. Последняя ревизия имеет явную связь с предшествующей; исходные двадцать моделей и история публикаций не заменяются.

## Минимальная подборка и публикация v1.3

Подборка объединяет 20 различных базовых моделей, фиксированный membership, один launch_context и общий бюджет $100, включая discovery и все дочерние assessment runs. Существующие отдельные run IDs допустимы как детали исполнения: они ссылаются на одну подборку/бюджетный scope, не превращают её в 20 независимых лимитов. Старые одиночные результаты, в том числе Calendly, сохраняются отдельно и не подмешиваются в новый рейтинг.

Для сайта сохраняется отдельный версионированный JSON-снимок: идентификатор/дата/статус подборки, сценарий, состав, счётчики, карточки и причины отсутствия оценки, разрешённые короткие фрагменты/источники/паспорта, итоговые группы рейтинга. Публикация заменяет указатель на готовый снимок атомарно; ошибка нового запуска не уничтожает предыдущий. Приватные исходные документы, полный аудит, provider payload и секреты в публичную проекцию не входят. Готовый снимок не доказывает final-статус каждой модели: partial/needs_review остаются видимыми. Полный редактор AnalysisRevision/MarketStudy/catalog и owner API ниже относятся к этапу после прототипа.

Совместимое уточнение паспортов: `Claim.method`, `confidence`, `confidence_reason`, `assumptions` допускают отсутствие в старых сохранённых данных; новые промпты запрашивают эти поля. Отсутствие старого значения показывается явно, не заполняется выдумкой. `Criterion.reason` оформляется как model_judgment с evidence IDs, известными датами, confidence и основанием; отсутствие документа не превращается в цитату. Handoff исключает авторские method/confidence/confidence_reason/assumptions и affected criterion из входа независимого Verifier; необходимые нейтральные предпосылки передаются текстом тезиса или отдельными input claims без авторской оценки.

## Общие правила и области принадлежности

Каждая сущность имеет id, schema_version, created_at. ID — непрозрачная уникальная строка своего типа. Время — ISO 8601 UTC. Деньги — decimal amount, currency и дата оценки. Поля обязательны, если явно не nullable. Неизвестное обозначается null/unknown с причиной; неизвестные enum и лишние поля структурированных ответов отклоняются.

run_id НЕ является общим обязательным полем. Глобальные сущности: Company, BusinessModel, ModelVariant, Source и SourceSnapshot, CatalogRevision. Study-сущности: MarketStudy, CoverageCell, DiscoveryCycle, CandidateBacklog. Run-сущности: Run, CollectionSnapshot, AssessmentScope, AnalysisRevision и все аналитические результаты. Аудит/бюджет/исполнение могут относиться к study до создания run: study_id обязателен, run_id nullable. ID из другой области допустим только через явную разрешённую связь.

## Каталог, исследование и запуск

| Сущность | Поля сверх общих |
|---|---|
| Company | name; canonical_domain; operating_markets; discovery_snapshot_ids |
| BusinessModel | name; core_job; value_mechanism; revenue_mechanism; primary_group_id; group_tags[]; taxonomy_version; first_seen_at; source_snapshot_ids |
| ModelVariant | base_model_id; customer_segment; offer; sales_channel; dependencies; differentiation_note; differentiation_snapshot_ids[]; analog_company_ids[]; first_seen_at |
| CatalogRevision | parent_id nullable; immutable model/variant/company version refs; taxonomy_version; created_by |
| MarketStudy | market_code; scope_version; directions; source_families; date_window; languages; query_plan; status active/paused/ready_to_close/closed; cumulative_budget; draft_revision_id; published_revision_id nullable; closed_at nullable |
| StudyRevision | study_id; parent_id nullable; immutable coverage/backlog/catalog/run revision refs; scope/config snapshot; closure_evidence; owner_reason nullable |
| CoverageCell | study_id; direction_id; source_family_id; query_ids; result_dispositions[]; status pending/in_progress/covered/blocked; gap_reason nullable |
| DiscoveryCycle | study_id; query_ids; newly_read_unique_pages; newly_discovered_unique_models; novelty_ratio nullable; unprocessed_result_ids[] |
| CandidateBacklog | study_id; company/product description; source_snapshot_ids[]; base_model_id nullable; variant_id nullable; state unclassified/new/selected/processed/deferred/duplicate; run_ids[]; deferral_reason nullable; duplicate_of nullable |
| Run | study_id; market_code; mode live/replay; config_snapshot/hash; execution_policy_revision; prompt_versions; provider/model IDs; launch_context/hash; status; started_at; finished_at nullable; parent_run_id nullable; collection_snapshot_id nullable; draft_revision_id nullable; published_revision_id nullable; control_version; active_elapsed_ms; active_interval_started_at nullable; discovery_complete; counts; budget_usage |
| CollectionSnapshot | run_id; catalog_revision_id; selection_policy_version; target_count; members[{base_model_id,variant_id,assessment_scope_id,primary_group_id,membership_rank,selection_reason}] |
| AssessmentScope | run_id; base_model_id; variant_id; market_code; launch_context_hash |

Discovery не требует искусственного run. Если Run создаётся до выбора участников, collection_snapshot_id остаётся null до фиксации. В CollectionSnapshot нет повторных base_model_id; после начала фильтров состав неизменяем. Новое описание/разделение варианта не меняет опубликованную CatalogRevision. Глобальная новизна базы и изученность пары variant/market/scenario — разные признаки; подробности 13-catalog.

## Документы и аналитические результаты

Source: canonical_url, publisher nullable. SourceSnapshot: source_id, requested_url, final_url, title, source_kind primary/secondary, published_at nullable, event_date nullable, fetched_at, fetch_status, body_ref nullable, content_hash nullable, extraction_method, origin_group_id nullable. Повторное чтение создаёт новый snapshot, даже при неизменном URL. Discovery связывает snapshots со study, AnalysisRevision явно разрешает доступ к выбранным snapshot IDs.

Все строки следующей таблицы содержат run_id, assessment_scope_id и analysis_revision_id помимо общего id/schema_version/created_at. model_id при выдаче API — alias base_model_id из scope.

| Сущность | Поля сверх общих и scope |
|---|---|
| Claim | text; neutral_text nullable; kind fact/calculation/judgment; scope(entity,geography,period nullable,metric nullable); method; evidence_ids[]; input_claim_ids[]; formula nullable; quantity_estimate_id nullable; affected_criterion_ids[]; confidence low/medium/high; confidence_basis; verification_status unverified/supported/refuted/mixed/insufficient/not_fact_checkable; checked_at nullable; uncertainty_reason nullable; review_question nullable; author_stage |
| EvidenceLink | claim_id; source_snapshot_id; excerpt; locator nullable; relation supports/refutes/context; relevance_note |
| CriterionResult | criterion_id; evaluation_state pending/running/completed/technical_failed; result pass/fail/unknown nullable; claim_ids[]; reason; unresolved_conflict_ids[]; evaluated_at nullable |
| Risk | neutral_claim_id; premise_claim_ids[]; essential_premise_ids[]; category; impact 1..3; likelihood_note; why_it_matters; proposed_test; critic_order |
| Verification | claim_id; status supported/refuted/mixed/insufficient/not_fact_checkable; evidence_ids[]; context_checks; explanation; remaining_question nullable; checked_at |
| RiskAssessment | risk_id; verification_ids[]; evidence_state supported_basis/refuted_basis/unresolved; explanation; checked_at |
| Conflict | claim_id; alternatives[{value_or_text,evidence_ids,dates}]; material_to_criteria[]; material_to_factors[]; status open/resolved; resolution nullable |
| QuantityEstimate | min; max; unit days/currency/count; currency nullable; as_of; start_event nullable; end_event nullable; assumption_claim_ids[]; estimate_kind fact/calculation/judgment |
| FactorAssessment | factor_id; value integer 0..4 nullable; score_low/high nullable; quantity_estimate_ids[]; supporting_claim_ids[]; rationale; missing_evidence[] |
| Score | config_hash; factor_assessment_ids[]; value nullable 0..100; range nullable; status final/provisional/unavailable; calculation_version; goal_feasibility feasible/uncertain/infeasible; feasibility_claim_ids[] |
| Card | eligibility; analysis_status; headline; claim_ids[]; risk_assessment_ids[]; score_id nullable; test_plan_claim_ids[]; limitations[] |

Evidence ID всегда означает EvidenceLink.id, а не Source.id или URL. Citation проверяется в конкретном snapshot. У новых записей LLM допустим temp_id в пакете и ссылки на temp_id того же пакета; приложение атомарно валидирует, назначает постоянные ID и переписывает все ссылки. Неизвестный ID/нарушение scope отклоняет пакет целиком. Роли не устанавливают серверные поля id/run/revision/timestamps: приложение заполняет их после валидации.

neutral_text обязателен для передаваемого тезиса критика; не содержит вердикта/уверенности/команд. Количественные оценки min/max конечны, min<=max; дни и деньги неотрицательны. Для денег обязательна currency; интервалы сроков консервативно округляются вверх до целых дней перед шкалой, исходные значения сохраняются. FactorAssessment.score_low/high — баллы, не дни/доллары. Вычисления по исходным числам не извлекают числа из текста.

method: primary_source / secondary_source / calculation / model_judgment / human_required. secondary_source остаётся предлагаемым расширением методов исходного задания (D-06), не скрытым изменением согласованных требований. Для calculation нужны formula/input_claim_ids; для model_judgment — предпосылки или явное допущение, внешняя ссылка может отсутствовать. Для human_required обязательны uncertainty_reason/review_question. checked_at=null при unverified, обязателен для завершённой проверки. Не путать checked_at с fetched_at или published_at. Confidence — качественная оценка доказательств, не вероятность истинности; high не ставится при существенном открытом конфликте.

## Ревизии и публикация

AnalysisRevision: run_id, parent_revision_id nullable, state building/sealed, immutable config/prompt/input snapshots, result manifest (точные ID всех аналитических результатов), allowed_source_snapshot_ids. sealed manifest не меняется. Исправление создаёт новую building revision; наследуемые неизменённые результаты могут входить в manifest по прежним ID с сохранением происхождения. Доступ разрешается по manifest и assessment scope, не переписыванием analysis_revision_id у старых записей.

Run.draft_revision_id указывает на текущий черновик; published_revision_id — на sealed snapshot. Publication: target_type run/study/catalog, target_id, revision_id, published_at, published_by. До публикации проверяются ссылки, паспорта, public projection и полнота manifest. Публикация partial run разрешена с явными неполными статусами; это не делает незавершённые модели final. Изменение указателя атомарно. StudyRevision/CatalogRevision публикуются отдельно. Новый глобальный каталог не изменяет CollectionSnapshot или старую публикацию.

ManualReview: study_id, run_id nullable, target_type/id, expected_revision_id, reviewer, decision, reason, evidence_ids[], discovery_snapshot_ids[], created_at, supersedes_id nullable. Для фактологического решения нужны EvidenceLink; для классификации до run — snapshots и причина, без ложной аналитической EvidenceLink. Ручная правка создает новую ревизию и инвалидирует зависимые результаты; правила продолжения агентов — 04-validation.

## Состояния и отображение

CriterionResult.result заполнен только при evaluation_state=completed; pending/running/technical_failed имеют result=null. unknown означает завершённое исследование с недостатком бизнес-доказательств, а не сбой API.

Eligibility: есть completed fail → fail; иначе есть незавершённый критерий → pending; иначе есть completed unknown → needs_review; иначе все обязательные completed pass → pass. Открытые unknown при технической неполноте показываются как вопросы дополнительно, не заменяют технический статус.

Analysis_status: pending/running/technical_incomplete/needs_review/complete. complete требует всех нужных стадий и определённых факторов; для неизвестного фактора или unresolved verification — needs_review; завершённая проверка с полным набором обоснованных диапазонов допускает complete+final. Fail может завершить конвейер до агентов; это complete по предписанному маршруту, но никогда ranked. Критик/проверяющий обязательны для каждой допущенной модели.

| Приоритет отображения | Условие | Группа UI |
|---|---|---|
| 1 | eligibility=fail | Отклонено |
| 2 | Не выполнены обязательные стадии / technical_incomplete / eligibility=pending | Не завершено |
| 3 | eligibility=needs_review или analysis_status=needs_review или Score=unavailable | Нужна проверка |
| 4 | eligibility=pass, analysis_status=complete, Score=provisional | Предварительная оценка |
| 5 | eligibility=pass, analysis_status=complete, Score=final | Итоговый рейтинг |

Статистика технических ошибок показывается отдельно даже у fail. Счётчики групп взаимно исключаются; score отсутствует до оценки и не равен нулю. Обоснованные интервалы допускаются в final: value равен нижней границе, диапазон виден; группы достижимости определены в 07-ranking.

StageExecution — журнал исполнения узла LangGraph и долговечная квитанция результата, не очередь переходов. Поля: study_id, run_id nullable, assessment_scope_id nullable, analysis_revision_id nullable, graph_version, thread_id, checkpoint_ns, node_name, invocation_key, input_hash/config_hash/prompt_version, state pending/running/succeeded/retry_wait/failed/paused, attempt_count, request_count, run_lease_token, output_ref nullable, error_category nullable. invocation_key включает run/scope/node/input/config/prompt/analysis_revision; для повторяемого discovery/tool-loop также cycle_id/iteration_id. Уникальность квитанции предотвращает повторную фиксацию результата при переисполнении узла. Истёкшая аренда не ставит стадии в самостоятельную очередь; восстановление определяется checkpoint и квитанциями по 08/10.

Run.status: selecting/queued/running/completed/partial/paused/failed. selecting включает discovery и фиксацию подборки; расходы уже относятся к run. completed — все выбранные модели получили конечный результат по своему маршруту, включая needs_review; partial — есть техническая неполнота или подборка меньше target; paused — ждёт вмешательства/бюджета; failed — нет пригодного результата при фатальной ошибке. Discovery-цель кандидатов и полнота CEO-подборки — отдельные флаги, не один счётчик.

BudgetReservation: study_id, run_id nullable, execution_id, request_id, amount_upper, currency, state reserved/settled/unknown/released, actual_amount nullable. AuditEvent: study_id, run_id nullable, execution_id nullable, timestamp, event_type, actor, payload_ref, sanitized_payload, previous_revision_id nullable.

## Инварианты

- Итоговый рейтинг: только eligibility=pass, analysis_status=complete, Score=final в одном scenario/config/revision.
- Все отображаемые содержательные факты, оценки и планы имеют Claim-паспорт; статические подписи исключаются.
- Утверждение не подтверждается наличием URL; evidence относится к прочитанному snapshot и нужному scope.
- Источник может переиспользоваться, применимость к другому сегменту/рынку проверяется заново.
- published revision и связанные manifest-объекты неизменяемы.
- Счётчики компаний, базовых моделей, вариантов, подборки и ранжированных вариантов различаются.

## Usage ledger v1.0

UsageRecord и PricingVersion — канонические сущности учёта по контракту 10-operations. UsageRecord связан с BudgetReservation по reservation_id/request_id; run/study ссылки и nullable-счётчики соблюдают область исполнения. Корректировки usage/тарифа журналируются; записи хранятся вместе с историей запусков и входят в backup. Данные провайдеров санитизируются до сохранения: authorization headers, API-ключи и credentials не входят в usage payload.

## Состояние LangGraph, baseline v1.1

Run получает graph_version, state_schema_version и coordinator_thread_id. GraphExecution: run_id, assessment_scope_id nullable, thread_id (уникальный непрозрачный ID), checkpoint_ns, graph_version, latest_checkpoint_id nullable, status, interrupt_refs[]. Координатор run и каждый assessment scope имеют отдельные thread_id; это идентификаторы исполнения, не общий чат LLM. Checkpoint ID используется для диагностики восстановления и никогда не заменяет AnalysisRevision или published_revision_id.

RunLease: run_id, token, expires_at, owner_id; единственная аренда исполнения run, включая дочерние графы. RunCommand: run_id, kind start/resume/pause, expected_control_version, idempotency_key, payload_ref, state pending/applied/rejected. Команды доставляют действия владельца worker; не определяют порядок аналитических стадий.

Граф хранит типизированное состояние: ссылки на результаты/доказательства, выбранный scope, версии и interrupt IDs. Полные чужие отчёты не попадают в общий messages-массив. GraphInterrupt хранит kind manual_review/budget/technical, scope, target_revision_id, question_ref и state pending/resolved/superseded. Решение владельца связано с ManualReview, evidence IDs и ожидаемой версией; несколько параллельных вопросов разрешаются по собственным IDs. Бизнес-needs_review, техническая пауза и статус графа — отдельные поля.

## Уточнения v1.2

Физическое хранилище — PostgreSQL по 08; контракты IDs/immutable revisions/денег сохраняются. FetchResult расширен provenance и parser metadata по 02. Run хранит снимок role_model_config: model ID, reasoning effort, output limit и prompt version для каждой роли; смена аналитической настройки создаёт новый run, не подменяет старый checkpoint. EvalRun хранит fixture/version, role, repetition, expected/actual, critical, status, usage refs и config hash; его расходы учитываются как purpose=eval с явными лимитами. ReleaseManifest содержит commit SHA, image digest, версии миграций/graph/state, backup ref и результаты проверок.
