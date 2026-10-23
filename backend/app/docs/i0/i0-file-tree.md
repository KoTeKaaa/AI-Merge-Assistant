# I0 — Дерево файлов модульного монолита (зона Артёма З)

> Правило подмены: каждый узкий интерфейс (`services/*`) скрывает реализацию за фиктивной границей. Соседний модуль подключается через `fake/fixture`, а не через дублирование логики внутри своей зоны. Старые решения (`BackgroundTasks`, OAuth App, экспорт `.js`) запрещены.

## Корневая структура пакета

```
backend/app/
├── api/                      # HTTP-контракт (FastAPI 0.142.4)
│   ├── __init__.py
│   ├── router_auth.py        # /auth/* (GitHub App, сессии, cookie, CSRF)
│   ├── router_installations.py
│   ├── router_repos.py
│   ├── router_runs.py        # /runs/* (202 при создании)
│   ├── router_uploads.py
│   ├── router_exports.py     # .feature / .py / ZIP (без .js)
│   ├── router_merge.py       # ручной + явный автомерж
│   ├── router_stats.py
│   ├── router_webhooks.py
│   └── dependencies.py       # auth, quota, stale-check
├── core/                     # конфигурация, безопасность, логирование
│   ├── __init__.py
│   ├── config.py             # Pydantic Settings v2; .env; без ключей в коде
│   ├── security.py           # CSRF, cookie, HMAC-verify, шифрование токенов
│   ├── logging.py            # без prompts, исходников, ключей
│   └── exceptions.py         # единая схема ошибок (см. i0-api-spec.md)
├── db/                       # SQLAlchemy 2.0 + Alembic 1.x
│   ├── __init__.py
│   ├── base.py               # DeclarativeBase
│   ├── session.py            # async sessionmaker
│   ├── models/               # таблицы (см. i0-database.md)
│   │   ├── user.py
│   │   ├── session.py
│   │   ├── github_connection.py
│   │   ├── installation.py
│   │   ├── repository.py
│   │   ├── run.py
│   │   ├── attempt.py
│   │   ├── scenario.py
│   │   ├── artifact.py
│   │   ├── coverage_report.py
│   │   ├── github_action.py
│   │   └── webhook_delivery.py
│   └── migrations/           # Alembic
├── schemas/                  # Pydantic v2 DTO (версия T1 явная)
│   ├── __init__.py
│   ├── version.py            # SchemaVersion = Literal["T1"]
│   ├── source_snapshot.py
│   ├── generation_context.py
│   ├── scenario_set.py
│   ├── test_bundle.py
│   ├── runner_job.py
│   ├── execution_report.py
│   ├── validation_feedback.py
│   ├── merge_decision.py
│   ├── github_operation_result.py
│   └── errors.py             # единый формат ошибок
├── worker/                   # Celery 5.6 + Redis 8 (без BackgroundTasks)
│   ├── __init__.py
│   ├── celery_app.py         # broker=redis, backend=postgresql
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── generation.py     # вызов LLM (без очереди внутри LLM)
│   │   ├── validation.py     # шлюз качества
│   │   ├── runner.py         # вызов sandbox (фиксированное задание)
│   │   ├── coverage.py       # нормализация покрытия
│   │   ├── merge.py          # проверка условий + вызов GitHub
│   │   └── webhook.py        # идемпотентная обработка
│   └── fixtures/
│       ├── fake_llm.py
│       ├── fake_runner.py
│       └── fake_github.py
└── services/                 # узкие интерфейсы (подмена через fake/fixture)
    ├── __init__.py
    ├── interfaces.py         # абстрактные протоколы/ABC для границ
    ├── github/
    │   ├── __init__.py
    │   ├── interface.py      # GitHubServiceProtocol
    │   ├── client.py         # реализация (GitHub App, user token)
    │   ├── fake.py            # fixture для тестов без сети
    │   └── errors.py         # GitHubOperationResult, безопасные причины
    ├── parsing/
    │   ├── __init__.py
    │   ├── interface.py      # ParsingServiceProtocol
    │   ├── parser.py         # ast + diff-контекст
    │   ├── fake.py
    │   └── errors.py
    ├── llm/
    │   ├── __init__.py
    │   ├── interface.py      # LLMServiceProtocol
    │   ├── adapter.py        # Gemini через google-genai 2.29.0
    │   ├── fake.py            # offline-тесты без сети
    │   └── errors.py         # квота, модель, таймаут
    ├── runner/
    │   ├── __init__.py
    │   ├── interface.py      # RunnerServiceProtocol
    │   ├── controller.py     # вызов sandbox Макса (без Docker socket в API)
    │   ├── fake.py
    │   └── errors.py         # окружение, таймаут, сбор
    ├── validation/
    │   ├── __init__.py
    │   ├── interface.py      # ValidationServiceProtocol
    │   ├── gateway.py        # статический шлюз (Иван)
    │   ├── fake.py
    │   └── errors.py         # невалидный артефакт, опасный путь
    └── export/
        ├── __init__.py
        ├── interface.py      # ExportServiceProtocol
        ├── builder.py        # .feature / .py / ZIP (без .js)
        ├── fake.py
        └── errors.py         # размер, путь, хеш
```

## Правила подмены на границе

| Граница | Реализация | Fake/Fixture | Что не дублируется |
|---|---|---|---|
| `services/github/` | `client.py` | `fake.py` | OAuth App (запрещено); используется GitHub App |
| `services/parsing/` | `parser.py` | `fake.py` | Разбор diff и AST — только здесь |
| `services/llm/` | `adapter.py` | `fake.py` | Промпты, Gherkin, pytest-генерация — только здесь |
| `services/runner/` | `controller.py` | `fake.py` | Sandbox-контроллер Макса; API не получает Docker socket |
| `services/validation/` | `gateway.py` | `fake.py` | Статические правила + `pytest --collect-only` |
| `services/export/` | `builder.py` | `fake.py` | `.feature`, `.py`, ZIP; `.js` запрещён |

## Ключевые ограничения архитектуры (из team-guide.md)

- **Без `BackgroundTasks`**: все фоновые этапы через `Celery` (`worker/tasks/`).
- **Без OAuth App**: вход через GitHub App (`services/github/client.py`); токены хранятся зашифрованно в БД (`db/models/user.py`).
- **Без `.js` экспорта**: `services/export/builder.py` генерирует только `.feature`, `.py`, ZIP.
- **Без произвольных команд в runner**: `RunnerJob` содержит фиксированный режим (`collect`/`test`), разрешённый профиль, лимиты; никаких секретов и произвольных команд.
- **Без смешивания состояний в `Run`**: состояние выполнения, вердикт (`passed`/`failed`/`not_applicable`), актуальность (`current`/`stale`) и статус мержа хранятся раздельно (`db/models/run.py`).
