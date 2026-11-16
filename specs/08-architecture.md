# 08. Архитектура, конфигурация и API

Статус: спецификация с дополнением v1.5.2 по поручению пользователя 16.11.2026; реализация нового контракта не выполнена этим шагом.

## Контракт исполнения и публикации v1.5.2

[15-conditional-scoring](15-conditional-scoring.md) имеет приоритет для новой политики допуска и рейтинга. В этом изменении обновляются спецификации: приложение, конфиги, миграции, production snapshots и платные исследования не меняются автоматически. `scoring_policy_version` входит в новый frozen config и публичный снимок; роли, state schema и graph version фиксируются согласованно. Legacy run возобновляется по исходной политике и своим schemas/hashes; новая политика требует нового run/revision с явным происхождением. Сохранённые evidence snapshots можно использовать только с исходными provenance и manifest refs, оплаченные receipts не переписываются и не считаются выполнением новых ролевых входов.

Детерминированный eligibility-агрегатор маршрутизирует confirmed/conditional через одинаковые обязательные Critic → neutral handoff → Verifier → reconciliation → factors → card validation. Разрешённые условные пробелы новой политики — F01/F03/F05–F07; F02/F04/F08/F09/F10 по-прежнему требуют pass. Unknown F03 после исследования допускает conditional только при явно заданных рынке/нашей роли и условии проверки до первого клиента. Подтверждённое требование собственной специальной лицензии блокирует конкретный сценарий; существенное неразрешённое противоречие о применимости найденного требования остаётся unresolved. Неисследованный/неописанный сценарий и технический сбой не становятся conditional. Blocked/unresolved сохраняют отдельную карточку состояния; предметный unresolved после завершённых критериев заканчивает маршрут как needs_review/unavailable и не становится технической неполнотой из-за незапущенных downstream-стадий. После новых доказательств eligibility пересчитывается до публикации. Условный score требует всех пяти обоснованных значений/диапазонов и завершённых проверок. Неполнота обязательных стадий при eligibility=confirmed/conditional даёт provisional вне рейтинга только при имеющемся предварительном расчёте; без него либо при отсутствии обоснованного фактора — unavailable. Открытые разрешённые условия хранятся в `eligibility_conditions[]` и видны вместе с их влиянием на сроки/затраты/факторы.

Другой рынок не является resume с изменённым полем: требуется новый явно зафиксированный launch_context/assessment scope и новый анализ критериев и S01–S05, связанный с исходным результатом по правилам run/revision. Исторические результаты и расходы сохраняются; прежний score не переносится в новый рынок. Аналоги/покупатели/публикации могут обосновать рынок-кандидат, но не автоматический legal pass; лицензии третьих лиц не заменяют оценку нашей роли. Версия v1.5 с прежним gate F03 не переинтерпретируется как v1.5.2.

Публичная проекция и будущий ranking API возвращают раздельные final/conditional rankings, внутри каждого — группы goal_feasibility; общего места нет. Выдача карточки, сравнения, экспорта и счётчиков использует ту же версию политики/ревизии. Legacy-снимок остаётся читаемым с исходными статусами и меткой версии; UI не превращает его прежний needs_review в conditional и не вычисляет отсутствующий балл. Существующие пути и углублённые анализы с parent/derived/supplemental связями сохраняются.

## Исполнение обзора юридических документов аналогов — v1.5.2

В research добавляется обязательная, явно учитываемая работа по поиску/чтению доступных релевантных Legal/Terms, Privacy, DPA, AUP и лицензионных документов каждого аналога из manifest, включая дополнительные компании/продукты. Используются существующие search/fetch adapters, SourceSnapshot, Claim/EvidenceLink, P02/P02V и receipts; новый автономный агент/провайдер не требуется. Результат `AnalogueLegalReview` по 03 связывает analogue_id, scope и revision с реальными действиями доступа и аналитическими выводами. Ключ идемпотентности/хеш входа учитывает конкретный analogue_id, пакет документов и версию политики; review одной компании не подменяет автоматически review другого продукта или scope.

До завершённого final/conditional card validation проверяет наличие completed review каждого используемого аналога. Требуется выполненный ограниченный поиск и анализ доступных релевантных материалов, а не обязательное нахождение документов. Coverage reviewed/insufficient/not_found/unavailable хранится отдельно от execution_state. Внешние 404, login, robots и таймауты конкретного URL после предусмотренных попыток — исходы доступа/gaps и могут завершить review как unavailable; их не обходят. Ошибка исполнения/анализа/валидации или потеря результата оставляют неполный этап, а не completed gap. Общие лимиты и паузы бюджета по 10 сохраняются; исчерпание бюджета до выполнения работы не считается отсутствием документов.

Completed not_found/unavailable/insufficient не создают автоматический business fail и сами по себе не запрещают conditional. Допуск и пять факторов проверяются по 15; необходимые дополнительные F01/F03-проверки используют адресные официальные источники в существующих лимитах. Terms фиксируют договорные заявления поставщика, не дают blanket lawful pass; software license, regulatory license, governing law и география обслуживания различаются в данных/валидации. Публичная проекция включает review refs, coverage, доступные ссылки/версии/даты, короткие релевантные фрагменты, границы применимости и gaps/recheck conditions без приватных payload. Старые snapshots без review не маркируются как выполненные по v1.5.2.

Fetch переиспользует разрешённые canonical URL/snapshot refs с исходным provenance, исключая повторные загрузки одного уже прочитанного материала в рамках допустимой актуальности. Один URL не доказывает неизменность версии; новая фактическая загрузка сохраняется отдельным snapshot. Общий документ может входить в несколько reviews с отдельной оценкой применимости. Обнаруженные после замораживания пакета новые документы/аналог требуют существующего механизма новой ревизии/run с lineage и пересчётом затронутых зависимостей, не изменения frozen inputs/receipts на месте. Это контракт будущей реализации; дополнительные платные вызовы, лимиты, миграции и деплой данным изменением не запускаются.

## Исполняемый шаг v1.4

Ручной CLI выбирает именно восемь failed-элементов существующей подборки и явно включает recovery. До выполнения проверяются lease/lock, отсутствие неоднозначных незавершённых оплачиваемых операций и доступный общий бюджет. Восстанавливаются frozen scope, config, prompt/schema hashes, исходные ответы и завершённые стадии; mass reset, подмена документов и повтор всех платных шагов запрещены. Исправленная валидация/repair выполняется с сохранённого места, корректный needs_review является аналитическим результатом, не техническим сбоем.

Затем создаётся отдельный Helpdesk follow-up с прежним base_model_id, parent run/collection, новым scope и тем же общим study ledger. Новый сценарий: Texas/Austin, два человека, $15 000 без зарплат на 90 дней; узкий B2B email helpdesk SaaS для взрослых бизнес-пользователей, подписка через внешнего PSP, без визитов клиентов, custody, финансовых/медицинских/государственных функций и продажи персональных данных. Интерком остаётся исходным примером; документированный Zendesk может быть дополнительным аналогом той же модели. Пакет документов замораживается до графа; новые факты требуют ревизии. Публикация нового результата связана с исходным снимком, не меняет членство двадцати моделей. Постоянный worker и полный редактор ревизий по-прежнему отложены.

Новые лимиты источников вводятся совместимой добавочной миграцией 007: разрешённый диапазон индекса source_queries расширяется с 0–3 до 0–12, позиции результатов — до 1–16. Существующие индексы/строки и ограничения legacy-конфигурации не меняются. Элемент manifest для приоритетных URL имеет отдельный явный kind/аудит и не выдаётся за платный Tavily-запрос; количество реальных поисков проверяется независимо.

## Исполняемый объём прототипа v1.3

Сохраняем существующие FastAPI/Jinja/небольшой JavaScript, PostgreSQL, LangGraph и адаптеры Astra/Tavily. Один ручной последовательный серверный процесс выполняет discovery и 20 assessment scopes с общими сценарием и бюджетом $100; постоянный worker и browser write API сейчас не реализуются. Существующие lease/checkpoint/receipt и журнал расходов продолжают обеспечивать возобновление без повторения сохранённых платных этапов. Дочерние run IDs допустимы, если их расходы входят в один collection/study scope.

Web читает отдельный безопасный JSON-снимок публикации по 03-data. Обязательные HTML-маршруты: `/`, `/runs/{id}`, `/runs/{id}/models/{model_id}`, `/runs/{id}/compare`, `/methodology`; неизвестный/неопубликованный ID даёт 404. Полноценный `/api/v1`, owner auth/write operations, каталог и study API ниже — контракты после прототипа, не новые требования текущего выпуска. Существующие health endpoints сохраняются. Caddy принимает публичный HTTP/HTTPS на TCP 80/443 и проксирует только разрешённые read-only маршруты web; БД и приватные файлы остаются закрыты. Дополнение v1.3.1 подключает `ai-builder-lab.com`, автоматический TLS, HTTP-доменный redirect и постоянное TLS-состояние по 10-operations; прежний HTTP по IP сохраняется как резервный доступ.

## Компоненты

- Python + FastAPI: API, авторизация владельца, выдача страниц.
- Jinja templates + небольшой JavaScript: UI и периодическое обновление статуса; отдельный frontend framework не обязателен.
- PostgreSQL на локальном persistent volume: сущности, RunCommand/RunLease, журнал StageExecution, события и ревизии; один worker, до двух параллельных внешних задач. База не лежит на сетевом диске.
- Файловое хранилище на томе: private snapshots и полные очищенные payload, public exports отдельно.
- LangGraph StateGraph выполняет фиксированный граф и условные ветвления. Worker читает долговечные команды запуска/возобновления run и вызывает граф; отдельного планировщика очереди стадий нет. Checkpoints сохраняются PostgreSQL checkpointer в отдельной схеме той же БД, не InMemorySaver.
- Адаптер LLM: structured generation + tool loop + usage/error normalization. Выбран OpenAI gpt-6-astra; версия/параметры фиксируются для run, качество проверяется eval.
- Адаптер поиска: Tavily Search; адаптер чтения — HTTP/text extraction. Провайдеры не встроены в бизнес-правила.
- Reverse proxy с TLS, web и worker в Docker Compose; без Redis/Celery/vector DB для текущего объёма.

Версии библиотек выбираются и фиксируются lockfile при реализации. Документ не обещает совместимость конкретных текущих версий, не проверенных интеграционно. Целевое хранилище v1.2 — PostgreSQL; SQLite текущего кода является промежуточной реализацией.

## Поток и транзакции

Web валидирует config, создаёт Run + RunCommand(start) в одной транзакции и возвращает 202. Worker захватывает RunLease короткой транзакцией и запускает LangGraph; переходы и возобновление принадлежат только LangGraph. StageExecution сохраняет аудит и квитанции узлов, но больше не ставит зависимые стадии в очередь. Бюджет по run+study резервируется до каждого физического API-вызова. Запись domain result/StageExecution receipt/audit выполняется одной транзакцией приложения при актуальной RunLease; checkpoint фиксируется отдельно после этого. Общая атомарность business commit и checkpoint commit не предполагается. Повтор узла с тем же invocation_key возвращает сохранённый результат без повторного API-запроса; незавершённый внешний вызов оставляет unknown reservation. Exactly-once billing не обещается.

Resume загружает checkpoint совместимой версии графа и переиспользует квитанции succeeded-узлов при совпадении invocation_key. Аналитические изменения инвалидируют зависимые результаты и создают AnalysisRevision/new run по 03/04. Политика исполнения (время/бюджет) версионируется отдельно и не меняет аналитические входы. active_elapsed_ms/active_interval_started_at не сбрасываются при resume. Публикация указывает на sealed manifest AnalysisRevision; partial публикация сохраняет неполные статусы. Смена указателя атомарна.

## API-контракты

Префикс /api/v1. Все write endpoints только owner, защита CSRF для cookie-сессии. Ответы JSON: data + request_id; ошибка: code, message, details без секретов. 400 неверный запрос, 401/403 доступ, 404 неизвестный ID, 409 конфликт состояния/версии, 422 невалидная конфигурация, 429 локальный лимит, 503 недоступность сервиса. Server-side пагинация list endpoints, limit 1..100.

| Method/path | Вход | Выход/поведение |
|---|---|---|
| GET /runs | cursor, limit | Только опубликованные run summaries для гостя; владельцу также черновики |
| GET /runs/{id} | revision_id nullable, view=published/draft | Статус, счётчики, scenario, control_version, revision_id |
| GET /runs/{id}/models | revision_id, query, group, buyer, revenue, market, status, sort, cursor, limit | Фильтрация всего снимка до пагинации; sort=score_asc/score_desc/name, не меняет места |
| GET /runs/{id}/ranking | revision_id | Для v1.5.2: scoring_policy_version, раздельные final/conditional rankings с возрастающим баллом внутри goal_feasibility-группы; provisional/unavailable отдельно без места; legacy по исходной версии |
| GET /runs/{id}/models/{model_id} | revision_id | Карточка из того же manifest с паспортами и разрешёнными источниками; для v1.5.2 — публичные обзоры юридических документов всех её аналогов по 03 |
| GET /runs/{id}/audit | cursor, limit | Публичная очищенная проекция журнала; private payload только владельцу |
| GET /runs/{id}/export | format=json/md | Опубликованный безопасный экспорт |
| POST /runs | config_version, mode, source_run_id nullable; Idempotency-Key | 202 run_id; тот же ключ+тело возвращает прежний run, другое тело с тем же ключом — 409 |
| POST /runs/{id}/resume | expected_control_version | 202; незавершённые стадии, без сброса лимитов; повтор не создаёт копии |
| POST /runs/{id}/pause | expected_control_version | Не запускать новые стадии, текущий вызов завершить/таймаутить и сохранить |
| POST /reviews | target, expected_revision_id, decision, reason, evidence_ids | Новая ManualReview/AnalysisRevision, инвалидирование и команда продолжения графа по 04; 409 при конфликте |
| POST /runs/{id}/publish | revision_id, expected_control_version | Проверка public projection/manifest и атомарная публикация sealed revision |
| GET /health/live | — | Процесс отвечает |
| GET /health/ready | — | БД/том доступны, конфиг валиден; не делает платных запросов |

Владелец входит через POST /session (пароль), выходит DELETE /session; неверный пароль — одинаковая безопасная ошибка. Секреты провайдеров не принимаются через public config API. Конфиг в v1 редактируется владельцем в versioned файлах, UI показывает снимок; web-редактор промптов и секретов не входит в v1.

GET без revision_id выбирает published_revision_id; владелец может запросить view=draft и получить draft revision ID. Последующие переходы, сравнение, аудит и экспорт передают этот ID, чтобы не смешивать снимки. Гость не читает неопубликованные ревизии (404), включая прямые ссылки; partial опубликованная ревизия допустима. control_version — версия изменяемого состояния Run, expected_revision_id — конкретный неизменяемый/текущий черновой snapshot цели; эти понятия не взаимозаменяемы.

## Группы конфигурации

schema_version; launch_context; discovery(target=20, directions, excluded_markets); criteria[]; scoring(factors, weights, rubrics, rounding); providers(role_model_ids, API endpoint allowlist); prompts(versions); limits(time, concurrency, requests, tokens, cost); source_policy; publishing. Новая версия дополнительно фиксирует scoring_policy_version и политику eligibility/условий/раздельных рейтингов по 15; legacy-сериализация не получает эти поля задним числом. Secret values только env/secret files, конфиг хранит имена переменных. Невалидный вес, отсутствующий model ID или отсутствующие ключи блокируют live run до расхода средств. Replay не требует ключей, но маркируется отдельно.

## Дополнение v0.2: стек для выбранного объёма и Aeza

Предложенный стек сохраняется: Python/FastAPI + Jinja/HTML/CSS + небольшой JavaScript, LangGraph в отдельном Python worker, PostgreSQL, Docker Compose. Reverse proxy — Caddy с TLS. Это позволяет сделать полноценные карточки, фильтры, сравнение, раскрытие доказательств и интерфейс владельца без отдельной SPA. Внешний вид Kelpie не требует определённого frontend-фреймворка. CSS-компоненты/токены описаны в 09-interface; реализация начата после утверждения baseline.

Развёртывание предполагает VPS на Aeza с доступом к Docker и постоянному диску, а не только размещение статических файлов. Выбран Aeza SWEs-2, Стокгольм, Ubuntu 24.04, 2 vCPU/4 GB RAM/60 GB NVMe. Базовый путь контейнеризации описан в [официальной документации FastAPI](https://fastapi.tiangolo.com/deployment/docker/). Не предполагаем наличие готового тарифа Aeza или доступность API из любой страны.

PostgreSQL используется с одним worker и максимум двумя параллельными assessment scopes; международный охват не требует распределённого исполнения. Масштабирование — по измеренной необходимости.

Дополнительные API для исследования рынков: GET /studies (опубликованные обзоры гостю), GET /studies/{id}/coverage (покрытие без приватных payload), POST /studies (owner: scope/config), POST /studies/{id}/close (owner: expected_revision, reason; требует ready_to_close), POST /studies/{id}/defer (owner: candidate IDs, reason). Открытие следующего рынка — новое owner-создание study после закрытия текущего; все исключения отражаются в истории. POST /runs получает study_id; его market/scenario сверяются со study. Обновления исследования версионируются и публикуются по правилам снимков, как run. Лимит денежных расходов задаётся также на study, чтобы много маленьких run не обходили общий предел.

## Дополнение v0.3: каталог и подборка

GET /catalog: опубликованные базовые модели с cursor/limit, query, group, buyer, revenue, launch_requirement, market, status; уникальный base_model_id возвращается один раз независимо от числа тегов. GET /catalog/{base_model_id}: варианты, компании-примеры и ссылки на опубликованные assessment scopes, без общего балла базы. Неоценённые записи явно помечены.

POST /runs фиксирует CollectionSnapshot после discovery/классификации и отбора; до фиксации stage=selecting, выбранные участники доступны владельцу. Ручная корректировка варианта/неоднозначного дубля фиксируется с причиной до запуска фильтров; после фиксации изменение состава создаёт новый run. Маршруты карточек по model_id в run однозначны, потому что в подборке один вариант каждой базы. В API результатов добавляется assessment_scope_id/variant_id. Ключи стадий и кэша из раздела транзакций включают scope.

Конфигурация получает catalog.taxonomy_version, group_definitions, filter_values, variant_rules и selection.policy_version/target/one_variant_per_base. Принципы и начальные значения — в 13-catalog. Отдельная vector DB или ML-кластеризация для v1 не требуется.

Дополнительные owner-операции: POST /catalog/reviews (expected_catalog_revision_id, merge/split/classify, IDs, reason, snapshot_ids) создаёт CatalogRevision; POST /runs/{id}/selection (expected_control_version, members, reason) разрешён только до фиксации, проверяет уникальность баз и релевантность вариантов; 20 баз — целевой размер полной подборки, меньший состав разрешён при явном подтверждении владельцем с причиной и признаком partial (A01 остаётся непройденным); POST /studies/{id}/publish и POST /catalog/publish (revision_id, expected_revision_id) публикуют явные manifests. Публикация run сама не публикует весь backlog/catalog. Новая версия catalog не меняет состав/паспорт старой подборки. Публичный GET /catalog получает только published catalog snapshot; owner может запросить конкретную draft revision.

POST /studies/{id}/scope (expected_revision_id, scope, reason) создаёт новую StudyRevision со всеми исключениями и незавершённым покрытием; автоматического закрытия/перехода нет. POST /runs/{id}/execution-policy (expected_control_version, limits, reason) явно продлевает ресурсы без изменения аналитического hash; study-budget меняется отдельной owner-операцией с expected_revision_id. Смена лимитов не обнуляет расход.

## Usage API v1.0

Owner-only GET /owner/usage и GET /owner/usage/export?format=csv|json с фильтрами date_from/date_to/study_id/run_id/agent/provider/model/purpose. Агрегация поверх UsageRecord/PricingVersion по 10-operations, без повторного прибавления settled reservations. Чтение расходов не вызывает платные API. Встроенные операции discovery/selection создают run до первого внешнего вызова, чтобы расходы подготовки входили в $25/run.

## Единственный механизм оркестрации, baseline v1.1

Используем библиотеку LangGraph локально внутри worker Aeza. LangSmith Cloud/Agent Server, отдельный управляемый сервис и OpenAI Agents SDK не требуются. Версии langgraph, langgraph-checkpoint-postgres и psycopg фиксируются lockfile после интеграционных проверок. Данные приложения и checkpoints находятся в отдельных схемах одной PostgreSQL DB. Сеть/LLM не выполняются под DB-lock. Checkpoint durability синхронная; business commit и checkpoint commit остаются отдельными транзакциями, даже в одной БД.

Владение исполнением run проверяется перед вызовом графа, каждым внешним запросом, фиксацией результата и записью checkpoint. Адаптер checkpointer должен отклонять запись устаревшего RunLease token; одна лишь проверка в начале узла недостаточна. Worker продлевает аренду, потеря аренды прекращает новые действия. Параллельные дочерние узлы действуют под текущей арендой run.

Fencing выполняется в PostgreSQL: захват/продление RunLease и защищённая запись блокируют одну строку RunLease через SELECT FOR UPDATE. Checkpointer-адаптер в той же транзакции и на том же connection проверяет token/expiry и сохраняет checkpoint, pending writes и связанные blobs; проверка отдельным запросом до вызова saver недостаточна. При потере аренды запись отклоняется. Совместимость транзакционного подключения штатного saver должна быть доказана A56 до интеграции; если его API этого не позволяет, требуется пересмотр интеграции, а не ослабление fencing. Внешний HTTP-вызов не удерживает DB lock: перед отправкой фиксируется разрешённая попытка; уже разрешённая попытка может завершиться после потери аренды и учитывается как возможный расход. Дубликаты блокируются квитанциями/unknown reservation. Web не пишет checkpoints; один worker остаётся ограничением v1.

Источники истины: checkpoints — позиция исполнения; application DB — бизнес-результаты, usage, immutable revisions и права; RunCommand — доставка управляющего действия. При сбое между domain commit и checkpoint узел повторяется и читает готовую квитанцию. При сбое после отправки платного запроса до записи результата неизвестный расход сохраняется; скрытая повторная отправка из-за replay запрещена. Дальнейший retry — отдельное зарегистрированное действие в рамках политики 10-operations.

Новая GraphExecution получает graph_version/state_schema_version, scope и свои checkpoint IDs. Изменение topology/schema не применяется молча к старым checkpoints: совместимость проверяется до resume; несовместимое исполнение остаётся paused до явной миграции либо нового run с parent_run_id. Старые публикации/usage сохраняются. UI/API продолжают использовать business statuses, revision_id и control_version; внутренние checkpoints не выдаются гостям.

Переход с текущего кода: Store.enqueue/claim/finish и постановка next_stage заменяются LangGraph runner + RunCommand/RunLease + идемпотентными node receipts. Два механизма планирования одновременно не эксплуатируются. Pricing, usage ledger, Pydantic, adapters, evidence и deterministic score переиспользуются; существующие tests стадий адаптируются к новым границам транзакций. Это требование к следующей реализации, не утверждение, что миграция уже выполнена.

## PostgreSQL, миграции и ресурсы v1.2

Приложение использует psycopg и параметризованный SQL; полноценный ORM не требуется. Миграции приложения — версионированные SQL-файлы, таблица schema_migrations с checksum и единый migration runner под advisory lock. Миграции saver выполняются его штатным setup отдельно до старта worker. Порядок и версии входят в release manifest; необратимые изменения требуют backup и плана восстановления. Web/worker не выполняют DDL автоматически при старте. DB port наружу не публикуется; runtime credentials не имеют административных прав, migrator использует отдельные права.

Резервирование бюджета блокирует study, затем run в одинаковом порядке; проверка остатка, вставка reservation и счётчиков — одна транзакция. Уникальные request_id/invocation_key/idempotency_key защищены DB constraints. SQLSTATE serialization/deadlock допускает ограниченный повтор только транзакции, не внешнего API. Деньги — целые microdollars либо точный NUMERIC, без float. Синхронные DB операции не блокируют event loop: использовать async psycopg и ограниченные pools.

Переход с SQLite: сохранить исходную БД, перенести IDs, связи, usage/reservations, ревизии и references на файлы, проверить суммы/количества/hash; checkpoints SQLite не объявляются совместимыми с PostgreSQL. Если рабочих данных ещё нет, допускается чистая инициализация с явно зафиксированным фактом; удаление существующих запусков не является миграцией. Текущий код не считается мигрированным после изменения документа.

Aeza 2 vCPU/4 GB остаётся стартовым вариантом, не гарантией производительности. Compose содержит web, worker, postgres и caddy. До batch измерить peak RAM, CPU, диск, DB connections и длительности на двух параллельных scopes; отсутствие OOM/restart и сохранение работоспособности web обязательны. Сборка образов выполняется CI, а не на VPS. Kubernetes/Redis/Celery/векторная DB/SPA не добавляются.

[Async psycopg](https://www.psycopg.org/psycopg3/docs/advanced/async.html): соединения не разделяются между процессами; каждый процесс создаёт свой pool. Для независимых параллельных транзакций используются разные connections, а не общий connection с несколькими cursors.
