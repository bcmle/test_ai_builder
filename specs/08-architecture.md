# 08. Архитектура, конфигурация и API

Статус: DRAFT v0.4. Предложение: один сервер, модульное приложение, отдельный worker. Не утверждённый стек.

## Компоненты

- Python + FastAPI: API, авторизация владельца, выдача страниц.
- Jinja templates + небольшой JavaScript: UI и периодическое обновление статуса; отдельный frontend framework не обязателен.
- SQLite в WAL на локальном persistent volume: сущности, очередь StageExecution, события и ревизии; один worker, до двух параллельных внешних задач. База не лежит на сетевом диске.
- Файловое хранилище на томе: private snapshots и полные очищенные payload, public exports отдельно.
- Worker выполняет ограниченный DAG. Очередь долговечна в БД, не только в памяти HTTP-процесса.
- Адаптер LLM: structured generation + tool loop + usage/error normalization. Предварительный кандидат Claude; точный model ID фиксируется после проверки доступа и eval, один на роль в пределах run.
- Адаптер поиска: предварительно Tavily; адаптер чтения — HTTP/text extraction. Провайдеры не встроены в бизнес-правила.
- Reverse proxy с TLS, web и worker в Docker Compose; без Redis/Celery/vector DB для текущего объёма.

Версии библиотек выбираются и фиксируются lockfile при реализации. Документ не обещает совместимость конкретных текущих версий, не проверенных интеграционно. Если SQLite упирается в измеренные ограничения, переход на PostgreSQL — отдельное изменение спеки.

## Поток и транзакции

Web валидирует config и создаёт Run + pending stages в одной транзакции, возвращает 202. Worker захватывает lease_token/lease_expires_at короткой транзакцией, освобождает DB-lock на время сети. Запись результата и следующей стадии атомарна и принимает только актуальный lease_token; устаревший ответ сохраняется в аудите без изменения результата. Уникальность stage по run/assessment_scope/stage/input_hash/analytical_config_hash/prompt_version предотвращает дублирование. Бюджет резервируется транзакционно по run+study до каждого физического запроса согласно 10-operations. Exactly-once billing не обещаем: при таймауте резерв unknown остаётся занятым.

Resume переиспользует succeeded stages при совпадении ключа. Аналитические изменения инвалидируют зависимые результаты и создают AnalysisRevision/new run по 03/04. Политика исполнения (время/бюджет) версионируется отдельно и не меняет аналитические входы. active_elapsed_ms/active_interval_started_at не сбрасываются при resume. Публикация указывает на sealed manifest AnalysisRevision; partial публикация сохраняет неполные статусы. Смена указателя атомарна.

## API-контракты

Префикс /api/v1. Все write endpoints только owner, защита CSRF для cookie-сессии. Ответы JSON: data + request_id; ошибка: code, message, details без секретов. 400 неверный запрос, 401/403 доступ, 404 неизвестный ID, 409 конфликт состояния/версии, 422 невалидная конфигурация, 429 локальный лимит, 503 недоступность сервиса. Server-side пагинация list endpoints, limit 1..100.

| Method/path | Вход | Выход/поведение |
|---|---|---|
| GET /runs | cursor, limit | Только опубликованные run summaries для гостя; владельцу также черновики |
| GET /runs/{id} | revision_id nullable, view=published/draft | Статус, счётчики, scenario, control_version, revision_id |
| GET /runs/{id}/models | revision_id, query, group, buyer, revenue, market, status, sort, cursor, limit | Фильтрация всего снимка до пагинации; sort=score_asc/score_desc/name, не меняет места |
| GET /runs/{id}/ranking | revision_id | Возрастающий final ranking; provisional отдельно |
| GET /runs/{id}/models/{model_id} | revision_id | Карточка из того же manifest с паспортами и разрешёнными источниками |
| GET /runs/{id}/audit | cursor, limit | Публичная очищенная проекция журнала; private payload только владельцу |
| GET /runs/{id}/export | format=json/md | Опубликованный безопасный экспорт |
| POST /runs | config_version, mode, source_run_id nullable; Idempotency-Key | 202 run_id; тот же ключ+тело возвращает прежний run, другое тело с тем же ключом — 409 |
| POST /runs/{id}/resume | expected_control_version | 202; незавершённые стадии, без сброса лимитов; повтор не создаёт копии |
| POST /runs/{id}/pause | expected_control_version | Не запускать новые стадии, текущий вызов завершить/таймаутить и сохранить |
| POST /reviews | target, expected_revision_id, decision, reason, evidence_ids | Новая ManualReview/AnalysisRevision, инвалидирование и очередь по 04; 409 при конфликте |
| POST /runs/{id}/publish | revision_id, expected_control_version | Проверка public projection/manifest и атомарная публикация sealed revision |
| GET /health/live | — | Процесс отвечает |
| GET /health/ready | — | БД/том доступны, конфиг валиден; не делает платных запросов |

Владелец входит через POST /session (пароль), выходит DELETE /session; неверный пароль — одинаковая безопасная ошибка. Секреты провайдеров не принимаются через public config API. Конфиг в v1 редактируется владельцем в versioned файлах, UI показывает снимок; web-редактор промптов и секретов не входит в v1.

GET без revision_id выбирает published_revision_id; владелец может запросить view=draft и получить draft revision ID. Последующие переходы, сравнение, аудит и экспорт передают этот ID, чтобы не смешивать снимки. Гость не читает неопубликованные ревизии (404), включая прямые ссылки; partial опубликованная ревизия допустима. control_version — версия изменяемого состояния Run, expected_revision_id — конкретный неизменяемый/текущий черновой snapshot цели; эти понятия не взаимозаменяемы.

## Группы конфигурации

schema_version; launch_context; discovery(target=12, directions, excluded_markets); criteria[]; scoring(factors, weights, rubrics, rounding); providers(role_model_ids, API endpoint allowlist); prompts(versions); limits(time, concurrency, requests, tokens, cost); source_policy; publishing. Secret values только env/secret files, конфиг хранит имена переменных. Невалидный вес, отсутствующий model ID или отсутствующие ключи блокируют live run до расхода средств. Replay не требует ключей, но маркируется отдельно.

## Дополнение v0.2: стек для выбранного объёма и Aeza

Предложенный стек сохраняется: Python/FastAPI + Jinja/HTML/CSS + небольшой JavaScript, отдельный Python worker, SQLite WAL, Docker Compose. Reverse proxy — Caddy с TLS. Это позволяет сделать полноценные карточки, фильтры, сравнение, раскрытие доказательств и интерфейс владельца без отдельной SPA. Внешний вид Kelpie не требует определённого frontend-фреймворка. CSS-компоненты/токены описаны в 09-interface; код пока не создаём.

Развёртывание предполагает VPS на Aeza с доступом к Docker и постоянному диску, а не только размещение статических файлов. Конкретный тариф/ОС/регион пользователь ещё не указал. Базовый путь контейнеризации описан в [официальной документации FastAPI](https://fastapi.tiangolo.com/deployment/docker/). Не предполагаем наличие готового тарифа Aeza или доступность API из любой страны.

SQLite остаётся для первого прототипа с одним worker и ограниченным числом одновременных задач; международный охват сам по себе не требует распределённой архитектуры. При росте числа одновременных запусков или измеренном конфликте записей — отдельная спецификация перехода на PostgreSQL. Нельзя обещать текущему варианту неограниченный мировой объём.

Дополнительные API для исследования рынков: GET /studies (опубликованные обзоры гостю), GET /studies/{id}/coverage (покрытие без приватных payload), POST /studies (owner: scope/config), POST /studies/{id}/close (owner: expected_revision, reason; требует ready_to_close), POST /studies/{id}/defer (owner: candidate IDs, reason). Открытие следующего рынка — новое owner-создание study после закрытия текущего; все исключения отражаются в истории. POST /runs получает study_id; его market/scenario сверяются со study. Обновления исследования версионируются и публикуются по правилам снимков, как run. Лимит денежных расходов задаётся также на study, чтобы много маленьких run не обходили общий предел.

## Дополнение v0.3: каталог и подборка

GET /catalog: опубликованные базовые модели с cursor/limit, query, group, buyer, revenue, launch_requirement, market, status; уникальный base_model_id возвращается один раз независимо от числа тегов. GET /catalog/{base_model_id}: варианты, компании-примеры и ссылки на опубликованные assessment scopes, без общего балла базы. Неоценённые записи явно помечены.

POST /runs фиксирует CollectionSnapshot после discovery/классификации и отбора; до фиксации stage=selecting, выбранные участники доступны владельцу. Ручная корректировка варианта/неоднозначного дубля фиксируется с причиной до запуска фильтров; после фиксации изменение состава создаёт новый run. Маршруты карточек по model_id в run однозначны, потому что в подборке один вариант каждой базы. В API результатов добавляется assessment_scope_id/variant_id. Ключи стадий и кэша из раздела транзакций включают scope.

Конфигурация получает catalog.taxonomy_version, group_definitions, filter_values, variant_rules и selection.policy_version/target/one_variant_per_base. Принципы и начальные значения — в 13-catalog. Отдельная vector DB или ML-кластеризация для v1 не требуется.

Дополнительные owner-операции: POST /catalog/reviews (expected_catalog_revision_id, merge/split/classify, IDs, reason, snapshot_ids) создаёт CatalogRevision; POST /runs/{id}/selection (expected_control_version, members, reason) разрешён только до фиксации, проверяет уникальность баз и релевантность вариантов; 10–15 баз — целевой размер полной подборки, меньший состав разрешён при явном подтверждении владельцем с причиной и признаком partial (A01 остаётся непройденным); POST /studies/{id}/publish и POST /catalog/publish (revision_id, expected_revision_id) публикуют явные manifests. Публикация run сама не публикует весь backlog/catalog. Новая версия catalog не меняет состав/паспорт старой подборки. Публичный GET /catalog получает только published catalog snapshot; owner может запросить конкретную draft revision.

POST /studies/{id}/scope (expected_revision_id, scope, reason) создаёт новую StudyRevision со всеми исключениями и незавершённым покрытием; автоматического закрытия/перехода нет. POST /runs/{id}/execution-policy (expected_control_version, limits, reason) явно продлевает ресурсы без изменения аналитического hash; study-budget меняется отдельной owner-операцией с expected_revision_id. Смена лимитов не обнуляет расход.
