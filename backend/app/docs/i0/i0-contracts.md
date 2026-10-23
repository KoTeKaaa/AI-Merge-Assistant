# I0 — Полная спецификация межмодульных контрактов (Pydantic v2, схема T1)

> Источник: раздел 4 `team-guide.md` (контракты между модулями). Каждое DTO содержит явное поле `schema_version: Literal["T1"]`. Никакой DTO не переносит `provider credentials` в пользовательский код.

---

## Общая версия схемы

```python
from pydantic import BaseModel, Field
from typing import Literal

SCHEMA_VERSION: Literal["T1"] = "T1"
```

---

## 1. `SourceSnapshot`

Производитель: GitHub-модуль / вход загрузки → потребители: `parsing`, оркестратор (`core`), `runner`.

```python
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, Literal
from uuid import UUID

class SourceSnapshot(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)

    # Источник
    source_type: Literal["github", "upload"] = Field(..., description="Вид источника")

    # ID прогона / снимка
    run_id: UUID = Field(..., description="ID прогона (Run)")
    snapshot_id: UUID = Field(..., description="ID снимка (Attempt или загруженный артефакт)")

    # Репозиторий (только для github)
    repo_id: Optional[UUID] = Field(None, description="ID репозитория из БД")
    repo_owner: Optional[str] = Field(None, min_length=1, max_length=255)
    repo_name: Optional[str] = Field(None, min_length=1, max_length=255)

    # SHA веток
    source_sha: str = Field(..., min_length=7, max_length=40, pattern=r"^[a-f0-9]{7,40}$")
    target_sha: str = Field(..., min_length=7, max_length=40, pattern=r"^[a-f0-9]{7,40}$")

    # Diff и содержимое
    diff_text: Optional[str] = Field(None, description="Текст diff (изменённые строки)")
    diff_artifact_path: Optional[str] = Field(None, description="Путь к артефакту diff на volume")

    # Фиксированное содержимое / ссылка на артефакт
    content_reference: str = Field(..., description="Ссылка на артефакт или фиксированное содержимое")
    content_hash: str = Field(..., min_length=32, max_length=128, description="SHA-256 содержимого")

    # Профиль окружения
    environment_profile: str = Field(..., min_length=1, max_length=128, description="Имя профиля зависимостей/окружения")
```

**Правила валидации:**
- `source_sha` и `target_sha` — только hex-строки длиной 7–40 (Git SHA).
- `content_hash` — SHA-256 (32–128 hex-символов).
- `repo_owner`/`repo_name` обязательны при `source_type == "github"`; при `upload` — `None`.
- `diff_text` и `diff_artifact_path` — хотя бы одно должно быть заполнено при `github`.

---

## 2. `GenerationContext`

Производитель: Иван (`parsing`) + оркестратор (`core`) → потребитель: `llm` (`services/llm/`).

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from uuid import UUID

class GenerationContext(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)

    # Снимок
    source_snapshot: SourceSnapshot = Field(...)

    # Режим
    mode: Literal["default", "lazy"] = Field(..., description="default требует описание; lazy выводит ожидания из diff")

    # Описание / требования
    description: Optional[str] = Field(None, max_length=10000)
    requirements_text: Optional[str] = Field(None, max_length=50000)

    # Diff (если доступен)
    diff_text: Optional[str] = Field(None)

    # Фрагменты и существующие тесты
    code_fragments: List[str] = Field(default_factory=list, description="Подготовленные фрагменты кода (AST-выборки)")
    existing_tests: List[str] = Field(default_factory=list, description="Текст существующих тестов")

    # ID источников
    source_ids: List[UUID] = Field(default_factory=list, description="ID источников (документы, репозитории)")

    # Разрешённые пути
    allowed_paths: List[str] = Field(default_factory=list, description="Разрешённые пути для генерации")

    # Профиль зависимостей
    dependency_profile: str = Field(..., min_length=1, max_length=128)
```

**Правила валидации:**
- `mode` — только `default` или `lazy`.
- `allowed_paths` — каждый элемент — относительный безопасный путь (без `..`, без абсолютных путей).
- `code_fragments` — не более 50 элементов; каждый не длиннее 20000 символов.

---

## 3. `ScenarioSet` и `TestBundle`

Производитель: `llm` (`services/llm/`) → потребители: шлюз (`validation`), оркестратор (`core`), экспорт (`services/export/`).

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from uuid import UUID

class ScenarioItem(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)
    scenario_id: UUID = Field(...)
    source_refs: List[UUID] = Field(..., description="Ссылки на источники")
    requirement_tag: Literal["requirements", "inferred"] = Field(..., description="Маркировка происхождения ожидания")
    gherkin_text: str = Field(..., min_length=1, max_length=20000)

class ScenarioSet(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)
    set_id: UUID = Field(...)
    run_id: UUID = Field(...)
    snapshot_id: UUID = Field(...)
    scenarios: List[ScenarioItem] = Field(..., min_items=1)
    prompt_model: str = Field(..., min_length=1, max_length=256, description="Модель/версия промпта")
    prompt_version: str = Field(..., min_length=1, max_length=32)
    usage: Literal["known", "unknown"] = Field(default="unknown", description="Usage либо unknown")
    set_hash: str = Field(..., min_length=32, max_length=128, description="Хеш набора сценариев")

class TestItem(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)
    test_path: str = Field(..., min_length=1, max_length=512, description="Путь к файлу теста")
    test_text: str = Field(..., min_length=1, max_length=50000)
    linked_scenario_ids: List[UUID] = Field(default_factory=list)

class TestBundle(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)
    bundle_id: UUID = Field(...)
    run_id: UUID = Field(...)
    snapshot_id: UUID = Field(...)
    tests: List[TestItem] = Field(..., min_items=1)
    bundle_hash: str = Field(..., min_length=32, max_length=128, description="Хеш набора тестов")
    prompt_model: str = Field(...)
    prompt_version: str = Field(...)
    usage: Literal["known", "unknown"] = Field(default="unknown")
```

**Правила валидации:**
- `scenario_id` и `test_path` — безопасные относительные пути (без `..`).
- `set_hash` / `bundle_hash` — SHA-256 набора (хеш содержимого, не пути).
- `usage` — `unknown`, если в ответе LLM отсутствует поле `usage`.
- `requirement_tag` — `requirements` для явных требований; `inferred` для lazy-режима.

---

## 4. `RunnerJob`

Производитель: оркестратор (`core`) → потребитель: `runner` (`services/runner/` → sandbox Макса).

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from uuid import UUID

class RunnerJob(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)

    # ID
    job_id: UUID = Field(...)
    run_id: UUID = Field(...)
    snapshot_id: UUID = Field(...)
    attempt_id: UUID = Field(..., description="Attempt, хранящий снимок и хеш тестов")

    # Хеш набора тестов
    test_bundle_hash: str = Field(..., min_length=32, max_length=128)

    # Артефакты (пути на volume)
    artifacts: List[str] = Field(default_factory=list, description="Пути к артефактам на volume")

    # Разрешённый профиль
    allowed_profile: str = Field(..., min_length=1, max_length=128)

    # Лимиты
    cpu_limit: int = Field(..., ge=1, le=8, description="CPU cores")
    memory_limit_mb: int = Field(..., ge=128, le=8192)
    timeout_seconds: int = Field(..., ge=30, le=600)
    pid_limit: int = Field(..., ge=10, le=500)
    disk_limit_mb: int = Field(..., ge=100, le=10000)

    # Фиксированный режим (без секретов и произвольных команд)
    mode: Literal["collect", "test"] = Field(..., description="collect — сбор; test — запуск тестов")
    fixed_command: Optional[str] = Field(None, max_length=1024, description="Фиксированная команда, не произвольная")
```

**Правила валидации:**
- `mode` — только `collect` или `test`.
- `fixed_command` — если задано, должно содержать только разрешённые подстроки (`pytest`, `coverage`, `python -m pytest`); никаких `curl`, `wget`, `bash -c`, `eval`, `exec`.
- `artifacts` — каждый путь — относительный безопасный путь (без `..`).
- Никаких полей `secret`, `token`, `password`, `env` с произвольным содержимым.

---

## 5. `ExecutionReport`

Производитель: `runner` (`services/runner/` / sandbox Макса) → потребители: оркестратор (`core`), шлюз (`validation`).

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict
from uuid import UUID

class ExecutionReport(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)

    # ID / хеши
    job_id: UUID = Field(...)
    run_id: UUID = Field(...)
    snapshot_id: UUID = Field(...)
    attempt_id: UUID = Field(...)
    test_bundle_hash: str = Field(..., min_length=32, max_length=128)

    # Digest образа
    image_digest: str = Field(..., min_length=32, max_length=128, description="Digest sandbox-образа")

    # Статус / exit code
    status: Literal["success", "failed", "timeout", "cancelled", "unknown"] = Field(...)
    exit_code: Optional[int] = Field(None, ge=-1, le=255)

    # Результаты сбора и тестов
    collect_result: Optional[Dict] = Field(None, description="Результат pytest --collect-only")
    test_result: Optional[Dict] = Field(None, description="Результат выполнения тестов")

    # Bounded-логи
    bounded_logs: List[str] = Field(default_factory=list, description="Ограниченные логи (без секретов)")
    log_limit_lines: int = Field(default=500, ge=1, le=5000)

    # Сырые отчёты покрытия
    raw_coverage_reports: List[str] = Field(default_factory=list, description="Пути к сырым .coverage или XML")

    # Причина сбоя / таймаута
    failure_reason: Optional[str] = Field(None, max_length=2000, description="Безопасная причина (без секретов)")
    timeout_reason: Optional[str] = Field(None, max_length=2000)
```

**Правила валидации:**
- `bounded_logs` — каждый элемент не длиннее 2000 символов; суммарно не более `log_limit_lines`.
- `failure_reason` — не содержит секретов, токенов, путей вне разрешённых.
- `raw_coverage_reports` — пути только на volume (без абсолютных системных путей).
- `status` — `unknown` только после сетевого обрыва или невозможности связаться с sandbox; требует сверки.

---

## 6. `ValidationFeedback`

Производитель: Иван (`validation`) / оркестратор (`core`) → потребитель: `llm` (`services/llm/`).

```python
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID

class ValidationFeedback(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)

    # ID снимка и тестов
    snapshot_id: UUID = Field(...)
    test_bundle_hash: str = Field(..., min_length=32, max_length=128)

    # Структурированные ошибки
    structured_errors: List[Dict] = Field(default_factory=list, description="Ошибки с типом, путём, сообщением")

    # Непокрытые изменённые строки
    uncovered_changed_lines: List[int] = Field(default_factory=list, description="Номера строк, изменённых и не покрытых")

    # Итерация
    iteration: int = Field(..., ge=1, le=10, description="Номер итерации доработки")

    # Оставшийся бюджет
    remaining_budget: int = Field(..., ge=0, le=10, description="Оставшиеся попытки доработки")
```

**Правила валидации:**
- `structured_errors` — каждый элемент содержит `type` (`model`, `environment`, `tests`, `quota`, `rights`, `stale`), `path`, `message`.
- `uncovered_changed_lines` — номера строк > 0; сортированы по возрастанию.
- `iteration` — не превышает `remaining_budget + 1`.
- Feedback от другого снимка или набора тестов отвергается на уровне оркестратора.

---

## 7. `MergeDecision`

Производитель: оркестратор (`core`) — **создаётся только сервером** → потребитель: `github` (`services/github/`).

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from uuid import UUID

class MergeDecision(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)

    # Владелец действия
    owner_user_id: UUID = Field(...)
    owner_github_login: str = Field(..., min_length=1, max_length=255)

    # ID прогона / попытки
    run_id: UUID = Field(...)
    attempt_id: UUID = Field(...)

    # Проверенные SHA / хеши
    verified_source_sha: str = Field(..., min_length=7, max_length=40)
    verified_target_sha: str = Field(..., min_length=7, max_length=40)
    verified_test_bundle_hash: str = Field(..., min_length=32, max_length=128)

    # Допуск или причины запрета
    allowed: bool = Field(...)
    denial_reasons: List[str] = Field(default_factory=list, description="Причины запрета (без утечки чужих данных)")

    # Режим
    mode: Literal["manual", "auto"] = Field(..., description="manual — основной; auto — только после явного включения")

    # Серверная метка
    server_created_at: str = Field(..., description="ISO-8601, только сервер")
```

**Правила валидации:**
- `allowed` — `True` только при `verified_target_sha` актуален, `test_bundle_hash` совпадает с текущим, `denial_reasons` пуст, и `mode` соответствует настройкам пользователя.
- `mode` — `auto` разрешён только если в БД (`Run`) установлен флаг `auto_merge_enabled` и целевая ветка защищена (`required_checks` пройдены).
- `server_created_at` — генерируется сервером; клиент не передаёт это поле.
- Объект никогда не создаётся клиентом напрямую; только через `POST /merge` с проверкой прав.

---

## 8. `GitHubOperationResult`

Производитель: `github` (`services/github/`) → потребитель: оркестратор (`core`).

```python
from pydantic import BaseModel, Field
from typing import Optional, Literal
from uuid import UUID

class GitHubOperationResult(BaseModel):
    schema_version: Literal["T1"] = Field(default="T1", frozen=True)

    # Тип операции
    operation_type: Literal["read_diff", "push_tests", "create_pr", "merge_pr", "check_status", "verify_webhook"] = Field(...)

    # Repo / branch / PR
    repo_owner: Optional[str] = Field(None, min_length=1, max_length=255)
    repo_name: Optional[str] = Field(None, min_length=1, max_length=255)
    branch_name: Optional[str] = Field(None, min_length=1, max_length=255)
    pr_number: Optional[int] = Field(None, ge=1)

    # Ожидаемый и фактический SHA
    expected_sha: Optional[str] = Field(None, min_length=7, max_length=40)
    actual_sha: Optional[str] = Field(None, min_length=7, max_length=40)

    # Статус
    status: Literal["success", "conflict", "forbidden", "unknown"] = Field(...)

    # Безопасная причина
    safe_reason: Optional[str] = Field(None, max_length=2000, description="Безопасная причина без утечки секретов")

    # ID для сверки повтора
    reconciliation_id: UUID = Field(..., description="ID для сверки повторной доставки")
```

**Правила валидации:**
- `status` — `unknown` требует сверки фактического состояния GitHub перед повтором записи; не вызывает немедленного повтора.
- `conflict` — `expected_sha` не совпадает с `actual_sha` или ветка изменилась.
- `forbidden` — недостаточно прав (`permissions` App или пользователя); не раскрывает внутренние ошибки GitHub.
- `reconciliation_id` — генерируется сервером; используется для идемпотентности повторов (`WebhookDelivery`).

---

## Общие правила для всех DTO

1. **Версия схемы**: каждое DTO содержит `schema_version: Literal["T1"]` (заморожено, не изменяется клиентом).
2. **Без секретов**: ни одно поле не содержит `token`, `secret`, `password`, `api_key`, `provider_key`.
3. **Без произвольных команд**: `RunnerJob` содержит только фиксированный режим и разрешённый профиль.
4. **Без `.js`**: `services/export/` не генерирует `.js`; только `.feature`, `.py`, ZIP.
5. **Без `BackgroundTasks`**: фоновые этапы через `Celery`; DTO передаются через очередь (`worker/tasks/`).
6. **Без OAuth App**: вход через GitHub App; `GitHubOperationResult` отражает это.
