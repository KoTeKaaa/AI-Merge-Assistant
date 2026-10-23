"""
GitHubIdempotencyManager — управление идемпотентными операциями с GitHub.

Правила (team-guide.md, разделы 4, 5, правило 10):
- Операции: push_tests (ai-tests/<run-id>), create_pr, merge_pr (с expected head SHA)
- Запись в исходную ветку и force-push ЗАПРЕЩЕНЫ
- Запись намерения (GitHubAction) ДО внешнего вызова
- Защита от дублей: при unknown статусе — сверка состояния в GitHub, НЕ немедленный повтор
- Stale снимок (изменился source_sha или target_sha) -> конфликт (409/conflict)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.schemas import (
    ErrorCategory,
    GitHubActionRecord,
    GitHubOperationResult,
    GitHubOperationStatus,
    GitHubOperationType,
)

logger = logging.getLogger(__name__)


class GitHubIdempotencyError(Exception):
    """Ошибка менеджера идемпотентности GitHub операций."""

    def __init__(self, category: ErrorCategory, message: str, status_code: int = 400):
        self.category = category
        self.status_code = status_code
        super().__init__(f"[{category.value}] {message}")


@dataclass(frozen=True, slots=True)
class IdempotencyCheckResult:
    """Результат проверки идемпотентности перед выполнением операции."""
    can_proceed: bool
    reason: str | None = None
    existing_action: GitHubActionRecord | None = None
    requires_reconciliation: bool = False
    reconciliation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Контекст для выполнения GitHub операции."""
    operation_type: GitHubOperationType
    owner_user_id: UUID
    owner_github_login: str
    run_id: UUID
    attempt_id: UUID
    repo_owner: str
    repo_name: str
    branch_name: str
    expected_source_sha: str
    expected_target_sha: str
    expected_head_sha: str | None = None  # для merge_pr
    pr_number: int | None = None
    idempotency_key: str | None = None


class GitHubIdempotencyManager:
    """
    Менеджер идемпотентности для GitHub write-операций.

    Обеспечивает:
    1. Запись намерения (GitHubAction) до внешнего вызова
    2. Проверку дублей и unknown-статусов (правило 10)
    3. Сверку фактического состояния в GitHub при unknown
    4. Блокировку при stale снимке (source_sha/target_sha изменились)
    """

    def __init__(
        self,
        action_repository: GitHubActionRepository,
        github_client: GitHubClient,
    ):
        """
        Args:
            action_repository: Репозиторий для хранения GitHubActionRecord
            github_client: Клиент для сверки состояния в GitHub (read-only операции)
        """
        self._actions = action_repository
        self._github = github_client

    # ==================== PUBLIC API ====================

    async def prepare_operation(
        self,
        context: OperationContext,
    ) -> IdempotencyCheckResult:
        """
        Подготовка операции: проверка идемпотентности, запись намерения.

        Вызывается ПЕРЕД любым внешним вызовом к GitHub API.

        Args:
            context: Контекст операции с ожидаемыми SHA и метаданными

        Returns:
            IdempotencyCheckResult: можно ли выполнять операцию

        Raises:
            GitHubIdempotencyError: при конфликте (stale) или ошибке прав
        """
        # 1. Генерируем/используем ключ идемпотентности
        idempotency_key = context.idempotency_key or self._generate_idempotency_key(context)

        # 2. Ищем существующее действие с таким ключом
        existing = await self._actions.find_by_idempotency_key(idempotency_key)

        if existing:
            return await self._handle_existing_action(existing, context)

        # 3. Проверяем актуальность снимка (stale check)
        stale_check = await self._check_snapshot_freshness(context)
        if not stale_check.can_proceed:
            return stale_check

        # 4. Создаём запись намерения (pending)
        action = GitHubActionRecord(
            id=uuid4(),
            user_id=context.owner_user_id,
            run_id=context.run_id,
            operation_type=context.operation_type,
            repo_owner=context.repo_owner,
            repo_name=context.repo_name,
            branch_name=context.branch_name,
            pr_number=context.pr_number,
            expected_sha=context.expected_head_sha or context.expected_source_sha,
            actual_sha=None,
            status=GitHubOperationStatus.UNKNOWN,  # pending = unknown до завершения
            safe_reason=None,
            reconciliation_id=uuid4(),
            initiator_user_id=context.owner_user_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        await self._actions.create(action)

        logger.info(
            "Recorded GitHub action intent",
            extra={
                "action_id": str(action.id),
                "operation": context.operation_type.value,
                "run_id": str(context.run_id),
                "idempotency_key": idempotency_key,
                "expected_sha": action.expected_sha,
            },
        )

        return IdempotencyCheckResult(
            can_proceed=True,
            existing_action=action,
            requires_reconciliation=False,
            reconciliation_id=action.reconciliation_id,
        )

    async def complete_operation(
        self,
        reconciliation_id: UUID,
        actual_sha: str,
        status: GitHubOperationStatus,
        safe_reason: str | None = None,
    ) -> GitHubActionRecord:
        """
        Завершение операции: обновление записи с фактическим результатом.

        Вызывается ПОСЛЕ успешного внешнего вызова к GitHub API.

        Args:
            reconciliation_id: ID сверки из prepare_operation
            actual_sha: Фактический SHA после операции
            status: Результат операции (success/conflict/forbidden/unknown)
            safe_reason: Безопасное описание причины (для логирования/UI)

        Returns:
            Обновлённая GitHubActionRecord
        """
        action = await self._actions.find_by_reconciliation_id(reconciliation_id)
        if not action:
            raise GitHubIdempotencyError(
                ErrorCategory.MODEL,
                f"Action record not found for reconciliation_id: {reconciliation_id}",
                status_code=404,
            )

        updated = GitHubActionRecord(
            id=action.id,
            user_id=action.user_id,
            run_id=action.run_id,
            operation_type=action.operation_type,
            repo_owner=action.repo_owner,
            repo_name=action.repo_name,
            branch_name=action.branch_name,
            pr_number=action.pr_number,
            expected_sha=action.expected_sha,
            actual_sha=actual_sha,
            status=status,
            safe_reason=safe_reason,
            reconciliation_id=action.reconciliation_id,
            initiator_user_id=action.initiator_user_id,
            created_at=action.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        await self._actions.update(updated)

        logger.info(
            "Completed GitHub action",
            extra={
                "action_id": str(action.id),
                "operation": action.operation_type.value,
                "status": status.value,
                "actual_sha": actual_sha,
            },
        )

        return updated

    async def reconcile_unknown(
        self,
        reconciliation_id: UUID,
    ) -> GitHubOperationResult:
        """
        Сверка состояния для операции со статусом unknown (правило 10).

        Вызывается при:
        - Повторной доставке Celery задачи
        - Повторном нажатии кнопки пользователем
        - Любом случае, когда предыдущий вызов вернул unknown

        Args:
            reconciliation_id: ID сверки из записи действия

        Returns:
            GitHubOperationResult с актуальным статусом из GitHub

        Raises:
            GitHubIdempotencyError: если снимок стал stale (409/conflict)
        """
        action = await self._actions.find_by_reconciliation_id(reconciliation_id)
        if not action:
            raise GitHubIdempotencyError(
                ErrorCategory.MODEL,
                f"Action record not found for reconciliation_id: {reconciliation_id}",
                status_code=404,
            )

        if action.status != GitHubOperationStatus.UNKNOWN:
            # Уже есть определённый результат — возвращаем его
            return GitHubOperationResult(
                operation_type=action.operation_type,
                repo_owner=action.repo_owner,
                repo_name=action.repo_name,
                branch_name=action.branch_name,
                pr_number=action.pr_number,
                expected_sha=action.expected_sha,
                actual_sha=action.actual_sha,
                status=action.status,
                safe_reason=action.safe_reason,
                reconciliation_id=action.reconciliation_id,
            )

        # Проверяем актуальность снимка перед сверкой
        context = OperationContext(
            operation_type=action.operation_type,
            owner_user_id=action.user_id,
            owner_github_login="",  # не нужно для read-only сверки
            run_id=action.run_id or uuid4(),
            attempt_id=uuid4(),  # не критично для сверки
            repo_owner=action.repo_owner or "",
            repo_name=action.repo_name or "",
            branch_name=action.branch_name or "",
            expected_source_sha=action.expected_sha or "",
            expected_target_sha="",  # будет проверено в _check_snapshot_freshness
        )

        stale_check = await self._check_snapshot_freshness(context)
        if not stale_check.can_proceed:
            # Снимок stale — обновляем запись и возвращаем conflict
            await self.complete_operation(
                reconciliation_id=reconciliation_id,
                actual_sha=action.expected_sha or "",
                status=GitHubOperationStatus.CONFLICT,
                safe_reason=stale_check.reason or "Snapshot became stale (source/target SHA changed)",
            )
            raise GitHubIdempotencyError(
                ErrorCategory.STALE,
                stale_check.reason or "Snapshot became stale",
                status_code=409,
            )

        # Выполняем сверку через GitHub клиент
        result = await self._github.reconcile_operation(action)

        # Обновляем запись с результатом сверки
        await self.complete_operation(
            reconciliation_id=reconciliation_id,
            actual_sha=result.actual_sha or action.expected_sha or "",
            status=result.status,
            safe_reason=result.safe_reason,
        )

        return result

    async def get_action_status(
        self,
        reconciliation_id: UUID,
    ) -> GitHubOperationResult | None:
        """Получить текущий статус операции по reconciliation_id."""
        action = await self._actions.find_by_reconciliation_id(reconciliation_id)
        if not action:
            return None

        return GitHubOperationResult(
            operation_type=action.operation_type,
            repo_owner=action.repo_owner,
            repo_name=action.repo_name,
            branch_name=action.branch_name,
            pr_number=action.pr_number,
            expected_sha=action.expected_sha,
            actual_sha=action.actual_sha,
            status=action.status,
            safe_reason=action.safe_reason,
            reconciliation_id=action.reconciliation_id,
        )

    # ==================== PRIVATE METHODS ====================

    async def _handle_existing_action(
        self,
        existing: GitHubActionRecord,
        context: OperationContext,
    ) -> IdempotencyCheckResult:
        """Обработка существующей записи действия (дубль/повтор)."""
        # Если операция уже завершена успешно — не повторяем
        if existing.status == GitHubOperationStatus.SUCCESS:
            return IdempotencyCheckResult(
                can_proceed=False,
                reason=f"Operation already completed successfully: {existing.operation_type.value}",
                existing_action=existing,
                requires_reconciliation=False,
            )

        # Если операция завершилась конфликтом или запретом — не повторяем
        if existing.status in (GitHubOperationStatus.CONFLICT, GitHubOperationStatus.FORBIDDEN):
            return IdempotencyCheckResult(
                can_proceed=False,
                reason=f"Operation previously failed with {existing.status.value}: {existing.safe_reason}",
                existing_action=existing,
                requires_reconciliation=False,
            )

        # Если статус unknown — требуется сверка (правило 10)
        if existing.status == GitHubOperationStatus.UNKNOWN:
            # Проверяем, не стал ли снимок stale
            stale_check = await self._check_snapshot_freshness(context)
            if not stale_check.can_proceed:
                # Обновляем существующую запись на conflict
                await self.complete_operation(
                    reconciliation_id=existing.reconciliation_id,
                    actual_sha=existing.expected_sha or "",
                    status=GitHubOperationStatus.CONFLICT,
                    safe_reason=stale_check.reason or "Snapshot became stale during reconciliation",
                )
                return IdempotencyCheckResult(
                    can_proceed=False,
                    reason=stale_check.reason or "Snapshot became stale",
                    existing_action=existing,
                    requires_reconciliation=False,
                )

            return IdempotencyCheckResult(
                can_proceed=False,
                reason="Previous operation status unknown — reconciliation required",
                existing_action=existing,
                requires_reconciliation=True,
                reconciliation_id=existing.reconciliation_id,
            )

        # Неожиданный статус
        return IdempotencyCheckResult(
            can_proceed=False,
            reason=f"Unexpected action status: {existing.status.value}",
            existing_action=existing,
            requires_reconciliation=False,
        )

    async def _check_snapshot_freshness(
        self,
        context: OperationContext,
    ) -> IdempotencyCheckResult:
        """
        Проверка актуальности снимка: source_sha и target_sha не должны измениться.

        Правило 5 раздела 5: "Новые коммиты, изменённые тесты и конфликт отменяют прежний допуск"
        """
        # Для push_tests проверяем source_sha (ветка ai-tests/<run-id> создаётся от source)
        # Для create_pr проверяем source_sha и target_sha
        # Для merge_pr проверяем target_sha и expected_head_sha (PR head)

        if context.operation_type in (
            GitHubOperationType.PUSH_TESTS,
            GitHubOperationType.CREATE_PR,
        ):
            # Проверяем source_sha
            actual_source_sha = await self._github.get_branch_sha(
                context.repo_owner,
                context.repo_name,
                context.branch_name if context.operation_type == GitHubOperationType.PUSH_TESTS
                else f"ai-tests/{context.run_id}",
            )
            if actual_source_sha and actual_source_sha != context.expected_source_sha:
                return IdempotencyCheckResult(
                    can_proceed=False,
                    reason=(
                        f"Source SHA changed: expected {context.expected_source_sha}, "
                        f"got {actual_source_sha}. Snapshot is stale."
                    ),
                )

        if context.operation_type in (
            GitHubOperationType.CREATE_PR,
            GitHubOperationType.MERGE_PR,
        ):
            # Проверяем target_sha
            actual_target_sha = await self._github.get_branch_sha(
                context.repo_owner,
                context.repo_name,
                "main",  # target branch — в контексте должна быть target ветка
            )
            # TODO: target branch name должен приходить в контексте
            # Для MVP используем main, но в production нужно передавать target_branch
            if actual_target_sha and actual_target_sha != context.expected_target_sha:
                return IdempotencyCheckResult(
                    can_proceed=False,
                    reason=(
                        f"Target SHA changed: expected {context.expected_target_sha}, "
                        f"got {actual_target_sha}. Snapshot is stale."
                    ),
                )

        # Проверяем, что PR head совпадает с expected_head_sha
        if (
            context.operation_type == GitHubOperationType.MERGE_PR
            and context.pr_number
            and context.expected_head_sha
        ):
            pr_head_sha = await self._github.get_pr_head_sha(
                context.repo_owner,
                context.repo_name,
                context.pr_number,
            )
            if pr_head_sha and pr_head_sha != context.expected_head_sha:
                return IdempotencyCheckResult(
                    can_proceed=False,
                    reason=(
                        f"PR head SHA changed: expected {context.expected_head_sha}, "
                        f"got {pr_head_sha}. Snapshot is stale."
                    ),
                )

        return IdempotencyCheckResult(can_proceed=True)

    @staticmethod
    def _generate_idempotency_key(context: OperationContext) -> str:
        """Генерация детерминированного ключа идемпотентности."""
        parts = [
            context.operation_type.value,
            str(context.owner_user_id),
            str(context.run_id),
            str(context.attempt_id),
            context.repo_owner,
            context.repo_name,
            context.branch_name,
            context.expected_source_sha,
            context.expected_target_sha,
        ]
        if context.pr_number:
            parts.append(str(context.pr_number))
        if context.expected_head_sha:
            parts.append(context.expected_head_sha)
        return "|".join(parts)


# ==================== ABSTRACT INTERFACES (for dependency injection) ====================

class GitHubActionRepository:
    """
    Абстрактный репозиторий для GitHubActionRecord.
    Реализация должна быть предоставлена через DI (например, SQLAlchemy/asyncpg).
    """

    async def create(self, action: GitHubActionRecord) -> None:
        """Создать запись намерения."""
        raise NotImplementedError

    async def update(self, action: GitHubActionRecord) -> None:
        """Обновить запись (статус, actual_sha, safe_reason)."""
        raise NotImplementedError

    async def find_by_idempotency_key(self, key: str) -> GitHubActionRecord | None:
        """Найти запись по ключу идемпотентности."""
        raise NotImplementedError

    async def find_by_reconciliation_id(self, reconciliation_id: UUID) -> GitHubActionRecord | None:
        """Найти запись по reconciliation_id."""
        raise NotImplementedError

    async def find_by_run_and_operation(
        self,
        run_id: UUID,
        operation_type: GitHubOperationType,
    ) -> list[GitHubActionRecord]:
        """Найти все действия для run и типа операции."""
        raise NotImplementedError


class GitHubClient:
    """
    Абстрактный клиент для read-only операций сверки с GitHub.
    Реализация использует GitHub App installation token.
    """

    async def get_branch_sha(
        self,
        repo_owner: str,
        repo_name: str,
        branch_name: str,
    ) -> str | None:
        """
        Получить текущий SHA ветки.
        Returns None если ветка не найдена.
        """
        raise NotImplementedError

    async def get_pr_head_sha(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
    ) -> str | None:
        """Получить head SHA открытого PR."""
        raise NotImplementedError

    async def reconcile_operation(
        self,
        action: GitHubActionRecord,
    ) -> GitHubOperationResult:
        """
        Сверка фактического состояния операции в GitHub.

        Для push_tests: проверяет, существует ли ветка ai-tests/<run-id> с ожидаемым SHA
        Для create_pr: проверяет, существует ли PR с ожидаемым head SHA
        Для merge_pr: проверяет, замержен ли PR с ожидаемым head SHA
        """
        raise NotImplementedError