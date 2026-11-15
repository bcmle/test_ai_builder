# 08. Архитектура, конфигурация и API

Статус: DRAFT v0.1. Предложение: один сервер, модульное приложение, отдельный worker. Не утверждённый стек.

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

Web валидирует config и создаёт Run + pending stages в одной транзакции, возвращает 202. Worker короткой транзакцией захватывает lease, освобождает транзакцию на время внешнего вызова, затем атомарно сохраняет результат/следующую стадию. DB-lock не держится во время сетевых операций. Уникальность stage по run/model/stage/input_hash/config_hash/prompt_version предотвращает дублирование результата. Повторный платный запрос после сетевого таймаута возможен: exactly-once billing не обещаем; такой расход отмечается unknown/estimated.

Resume переиспользует succeeded stages при совпадении ключа. Изменение входов/промпта/критериев делает кэш недействительным для зависимых этапов и создаёт новую ревизию/новый run. Ручное разрешение фактов создаёт новую derived revision; публикация атомарно переключает указатель на полностью валидированную ревизию.

## API-контракты

Префикс /api/v1. Все write endpoints только owner, защита CSRF для cookie-сессии. Ответы JSON: data + request_id; ошибка: code, message, details без секретов. 400 неверный запрос, 401/403 доступ, 404 неизвестный ID, 409 конфликт состояния/версии, 422 невалидная конфигурация, 429 локальный лимит, 503 недоступность сервиса. Server-side пагинация list endpoints, limit 1..100.

| Method/path | Вход | Выход/поведение |
|---|---|---|
| GET /runs | cursor, limit | Только опубликованные run summaries для гостя; владельцу также черновики |
| GET /runs/{id} | — | Статус, счётчики, scenario, published revision |
| GET /runs/{id}/models | status, cursor, limit | Карточки состояния и краткие оценки |
| GET /runs/{id}/ranking | — | Возрастающий final ranking; provisional отдельно |
| GET /runs/{id}/models/{model_id} | — | Карточка с паспортами и разрешёнными источниками |
| GET /runs/{id}/audit | cursor, limit | Публичная очищенная проекция журнала; private payload только владельцу |
| GET /runs/{id}/export | format=json/md | Опубликованный безопасный экспорт |
| POST /runs | config_version, mode, source_run_id nullable; Idempotency-Key | 202 run_id; тот же ключ+тело возвращает прежний run, другое тело с тем же ключом — 409 |
| POST /runs/{id}/resume | expected_revision | 202; только незавершённые стадии; повтор не создаёт копии |
| POST /runs/{id}/pause | expected_revision | Не запускать новые стадии, текущий вызов завершить/таймаутить и сохранить |
| POST /reviews | target, expected_revision, decision, reason, evidence_ids | Новая ManualReview и пересчёт зависимостей; 409 при устаревшей ревизии |
| POST /runs/{id}/publish | expected_revision | Проверка public projection и атомарная публикация выбранной ревизии |
| GET /health/live | — | Процесс отвечает |
| GET /health/ready | — | БД/том доступны, конфиг валиден; не делает платных запросов |

Владелец входит через POST /session (пароль), выходит DELETE /session; неверный пароль — одинаковая безопасная ошибка. Секреты провайдеров не принимаются через public config API. Конфиг в v1 редактируется владельцем в versioned файлах, UI показывает снимок; web-редактор промптов и секретов не входит в v1.

## Группы конфигурации

schema_version; launch_context; discovery(target=12, directions, excluded_markets); criteria[]; scoring(factors, weights, rubrics, rounding); providers(role_model_ids, API endpoint allowlist); prompts(versions); limits(time, concurrency, requests, tokens, cost); source_policy; publishing. Secret values только env/secret files, конфиг хранит имена переменных. Невалидный вес, отсутствующий model ID или отсутствующие ключи блокируют live run до расхода средств. Replay не требует ключей, но маркируется отдельно.
