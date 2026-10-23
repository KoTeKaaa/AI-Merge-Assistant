# I0 — Архитектура базы данных (PostgreSQL 17 + SQLAlchemy 2.0 + Alembic 1.x)

> Источник: разделы 3, 4, 5, 6 `team-guide.md`. Модели хранятся в `db/models/`. Миграции — `db/migrations/` (Alembic). Все таблицы используют `DeclarativeBase` из `db/base.py`.

---

## Общие правила

- **Первичные ключи**: `UUID` (PostgreSQL `uuid`) или `BigInteger` с автоинкрементом (`Sequence`). Для внешних ссылок предпочтительнее `UUID`.
- **Индексы**: `GIN` для текстовых полей (`diff_text`, `requirements_text`); `B-tree` для внешних ключей и состояний; составные индексы для запросов по `run_id + status`.
- **Без секретов в БД**: токены хранятся зашифрованно (`security.encrypt_token`); ключ шифрования отдельно от БД.
- **Дедупликация webhook**: `WebhookDelivery` с `delivery_id` (HMAC-проверка до записи).

---

## Таблица `User`

```python
from sqlalchemy import Column, String, DateTime, Boolean
from sqlalchemy.dialects.postgresql import UUID
import uuid

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_login = Column(String(255), nullable=False, unique=True, index=True)
    github_id = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=True)
    email = Column(String(255), nullable=True)
    encrypted_access_token = Column(String(1024), nullable=True)  # зашифровано
    encrypted_refresh_token = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
```

**Индексы:** `github_login` (unique), `github_id` (unique), `created_at` (B-tree).

---

## Таблица `Session`

```python
class Session(Base):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_token_hash = Column(String(256), nullable=False, unique=True, index=True)
    csrf_token = Column(String(256), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_activity_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `user_id`, `session_token_hash` (unique), `expires_at` (для очистки).

---

## Таблица `GitHubConnection`

```python
class GitHubConnection(Base):
    __tablename__ = "github_connections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    connection_type = Column(String(32), nullable=False)  # "user_token" или "app_installation"
    encrypted_token = Column(String(1024), nullable=True)
    github_app_installation_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `user_id`, `connection_type`.

---

## Таблица `Installation`

```python
class Installation(Base):
    __tablename__ = "installations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_app_installation_id = Column(String(255), nullable=False, unique=True, index=True)
    github_account_login = Column(String(255), nullable=False, index=True)
    github_account_type = Column(String(32), nullable=False)  # "User" или "Organization"
    permissions = Column(String(1024), nullable=True)  # JSON-строка прав
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `github_app_installation_id` (unique), `github_account_login`.

---

## Таблица `Repository`

```python
class Repository(Base):
    __tablename__ = "repositories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_repo_id = Column(String(255), nullable=False, unique=True, index=True)
    owner_login = Column(String(255), nullable=False, index=True)
    repo_name = Column(String(255), nullable=False, index=True)
    full_name = Column(String(512), nullable=False, index=True)
    is_public = Column(Boolean, default=True, nullable=False)
    default_branch = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** составной `(owner_login, repo_name)`; `full_name` (unique или index); `github_repo_id` (unique).

---

## Таблица `Run`

> Критическое требование: состояния выполнения, вердикт, актуальность и статус мержа хранятся **раздельно**.

```python
from sqlalchemy import Enum

class Run(Base):
    __tablename__ = "runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Источник
    source_type = Column(String(32), nullable=False)  # "github" или "upload"
    repo_id = Column(UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    source_sha = Column(String(40), nullable=False, index=True)
    target_sha = Column(String(40), nullable=False, index=True)

    # Состояние выполнения (из раздела 4 team-guide.md)
    execution_state = Column(
        Enum(
            "queued", "preparing", "generating", "validating",
            "running", "checking_coverage", "improving",
            "completed", "failed", "cancelled",
            name="run_execution_state"
        ),
        nullable=False,
        default="queued",
        index=True
    )

    # Вердикт (отдельно от состояния выполнения)
    verdict = Column(
        Enum("passed", "failed", "not_applicable", name="run_verdict"),
        nullable=True,
        index=True
    )

    # Актуальность (отдельно)
    freshness = Column(
        Enum("current", "stale", name="run_freshness"),
        nullable=False,
        default="current",
        index=True
    )

    # Статус мержа (отдельно)
    merge_status = Column(
        Enum("pending", "allowed", "denied", "completed", "conflict", name="run_merge_status"),
        nullable=False,
        default="pending",
        index=True
    )

    # Режим
    mode = Column(String(32), nullable=False)  # "default" или "lazy"
    auto_merge_enabled = Column(Boolean, default=False, nullable=False)

    # Профиль окружения и зависимостей
    environment_profile = Column(String(128), nullable=False)
    dependency_profile = Column(String(128), nullable=False)

    # Бюджеты и лимиты
    max_iterations = Column(Integer, nullable=False, default=3)
    remaining_budget = Column(Integer, nullable=False, default=3)

    # Временные метки
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
```

**Индексы:**
- `user_id` (B-tree)
- `execution_state` (B-tree)
- `verdict` (B-tree)
- `freshness` (B-tree)
- `merge_status` (B-tree)
- Составной `(user_id, execution_state, created_at)` для дашборда.
- `source_sha` + `target_sha` (для проверки актуальности).

---

## Таблица `Attempt`

> Хранит снимок (`SourceSnapshot`) и хеш тестов (`TestBundle`). `CoverageReport` связан с конкретной `Attempt`.

```python
class Attempt(Base):
    __tablename__ = "attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)

    # Снимок
    snapshot_id = Column(UUID(as_uuid=True), nullable=False, unique=True, index=True)
    source_sha = Column(String(40), nullable=False)
    target_sha = Column(String(40), nullable=False)
    content_hash = Column(String(128), nullable=False, index=True)

    # Хеш набора тестов
    test_bundle_hash = Column(String(128), nullable=False, index=True)

    # Профиль
    environment_profile = Column(String(128), nullable=False)

    # Состояние
    attempt_state = Column(
        Enum("pending", "running", "completed", "failed", "cancelled", name="attempt_state"),
        nullable=False,
        default="pending",
        index=True
    )

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
```

**Индексы:** `run_id`, `test_bundle_hash`, `content_hash`, `attempt_state`.

---

## Таблица `Scenario`

```python
class Scenario(Base):
    __tablename__ = "scenarios"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id = Column(UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)

    scenario_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    source_refs = Column(String(1024), nullable=False)  # JSON-массив UUID
    requirement_tag = Column(String(32), nullable=False)  # "requirements" или "inferred"
    gherkin_text = Column(Text, nullable=False)
    prompt_model = Column(String(256), nullable=False)
    prompt_version = Column(String(32), nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `attempt_id`, `run_id`, `scenario_id`.

---

## Таблица `Artifact`

> В БД хранятся пути на volume, размеры, хеши, принадлежность пользователю и срок хранения.

```python
class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    attempt_id = Column(UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="SET NULL"), nullable=True, index=True)

    # Путь на volume
    volume_path = Column(String(1024), nullable=False, unique=True, index=True)
    relative_path = Column(String(1024), nullable=False)

    # Размеры и хеши
    size_bytes = Column(BigInteger, nullable=False, default=0)
    content_hash = Column(String(128), nullable=False, index=True)

    # Тип
    artifact_type = Column(String(64), nullable=False)  # "feature", "py", "zip", "coverage_xml", "log"

    # Принадлежность и срок хранения
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    retention_until = Column(DateTime(timezone=True), nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `user_id`, `volume_path` (unique), `content_hash`, `retention_until` (для очистки), `artifact_type`.

---

## Таблица `CoverageReport`

> Связан с конкретной `Attempt` (которая хранит снимок и хеш тестов).

```python
class CoverageReport(Base):
    __tablename__ = "coverage_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id = Column(UUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)

    # Покрытие
    covered_changed_lines = Column(Integer, nullable=False, default=0)
    executable_changed_lines = Column(Integer, nullable=False, default=0)
    coverage_percentage = Column(Float, nullable=True)  # вычисляется; None при нулевом знаменателе

    # Файлы
    covered_files = Column(String(2048), nullable=True)  # JSON-массив путей
    uncovered_changed_lines = Column(String(2048), nullable=True)  # JSON-массив номеров строк

    # Сырые отчёты
    raw_coverage_path = Column(String(1024), nullable=True)

    # Вердикт покрытия
    coverage_verdict = Column(
        Enum("passed", "failed", "not_applicable", name="coverage_verdict"),
        nullable=False,
        default="not_applicable",
        index=True
    )

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `attempt_id` (unique), `run_id`, `coverage_verdict`.

---

## Таблица `GitHubAction`

```python
class GitHubAction(Base):
    __tablename__ = "github_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)

    # Операция
    operation_type = Column(String(64), nullable=False)  # "read_diff", "push_tests", "create_pr", "merge_pr", "check_status"
    repo_owner = Column(String(255), nullable=True)
    repo_name = Column(String(255), nullable=True)
    branch_name = Column(String(255), nullable=True)
    pr_number = Column(Integer, nullable=True)

    # Ожидаемые и фактические SHA
    expected_sha = Column(String(40), nullable=True)
    actual_sha = Column(String(40), nullable=True)

    # Статус и безопасная причина
    status = Column(String(32), nullable=False)  # "success", "conflict", "forbidden", "unknown"
    safe_reason = Column(String(2000), nullable=True)

    # Сверка повтора
    reconciliation_id = Column(UUID(as_uuid=True), nullable=False, unique=True, index=True)

    # Инициатор
    initiator_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `user_id`, `run_id`, `reconciliation_id` (unique), `status`, `initiator_user_id`.

---

## Таблица `WebhookDelivery`

> Дедупликация: `delivery_id` проверяется до обработки; неверная подпись не вызывает изменений.

```python
class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delivery_id = Column(String(255), nullable=False, unique=True, index=True)  # GitHub delivery GUID
    event_type = Column(String(64), nullable=False)  # "push", "pull_request", "installation", "installation_repositories"
    payload_hash = Column(String(128), nullable=False)  # HMAC-SHA256 исходных bytes
    signature_valid = Column(Boolean, nullable=False, default=False)
    processed = Column(Boolean, nullable=False, default=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
```

**Индексы:** `delivery_id` (unique), `event_type`, `signature_valid`, `processed`.

---

## Связи и ограничения целостности

| Таблица | Внешний ключ | On delete | Индекс |
|---|---|---|---|
| `Session` → `User` | `user_id` | `CASCADE` | `user_id` |
| `GitHubConnection` → `User` | `user_id` | `CASCADE` | `user_id` |
| `Installation` | — | — | `github_app_installation_id` (unique) |
| `Repository` | — | — | `github_repo_id` (unique) |
| `Run` → `User` | `user_id` | `CASCADE` | `user_id` |
| `Run` → `Repository` | `repo_id` | `SET NULL` | `repo_id` |
| `Attempt` → `Run` | `run_id` | `CASCADE` | `run_id` |
| `Scenario` → `Attempt` | `attempt_id` | `CASCADE` | `attempt_id` |
| `Scenario` → `Run` | `run_id` | `CASCADE` | `run_id` |
| `Artifact` → `User` | `user_id` | `CASCADE` | `user_id` |
| `Artifact` → `Run` | `run_id` | `SET NULL` | `run_id` |
| `Artifact` → `Attempt` | `attempt_id` | `SET NULL` | `attempt_id` |
| `CoverageReport` → `Attempt` | `attempt_id` | `CASCADE` | `attempt_id` (unique) |
| `CoverageReport` → `Run` | `run_id` | `CASCADE` | `run_id` |
| `GitHubAction` → `User` | `user_id` | `CASCADE` | `user_id` |
| `GitHubAction` → `Run` | `run_id` | `SET NULL` | `run_id` |
| `WebhookDelivery` | — | — | `delivery_id` (unique) |

---

## Правила хранения и очистки

- `Artifact.retention_until` — срок хранения; очистка через Celery-задачу (`cleanup_artifacts`).
- `Session.expires_at` — сессии удаляются после истечения срока.
- `WebhookDelivery` — записи удаляются через 30 дней после `processed_at`.
- `Run` с `execution_state = "cancelled"` или `"failed"` — не удаляются автоматически; используются для статистики и повторной проверки.
