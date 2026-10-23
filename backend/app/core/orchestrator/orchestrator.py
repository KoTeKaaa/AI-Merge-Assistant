"""
RunOrchestrator — конечный автомат оркестрации прогонов.

Правила (team-guide.md, разделы 3, 4, 5, 9):
- Состояния выполнения: queued -> preparing -> generating -> validating -> running -> checking_coverage -> improving -> completed / failed / cancelled
- Состояние выполнения, вердикт (passed/failed/not_applicable), актуальность (current/stale) и статус мержа хранятся РАЗДЕЛЬНО
- ASSUMPTION T3: начальная генерация + максимум 2 итерации доработки (improving), таймаут 120с на этап, 60с на запрос, макс 3 транспортные попытки
- ValidationFeedback должен соответствовать текущему snapshot_id и test_bundle_hash (чужой отвергается)
- Причины ошибок: model, environment, tests, quota, rights, stale
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from app.schemas import (
    MAX_IMPROVING_ITERATIONS,
    VALID_TRANSITIONS,
    CoverageReportDTO,
    ErrorCategory,
    ExecutionState,
    Freshness,
    MergeStatus,
    ValidationFeedback,
    Verdict,
)

logger = logging.getLogger(__name__)


class OrchestratorError(Exception):
    """Базовая ошибка оркестратора."""
    def __init__(self, category: ErrorCategory, message: str, state: ExecutionState | None = None):
        self.category = category
        self.state = state
        super().__init__(f"[{category.value}] {message}")


class InvalidTransitionError(OrchestratorError):
    """Недопустимый переход состояния."""
    def __init__(self, from_state: ExecutionState, to_state: ExecutionState):
        super().__init__(
            ErrorCategory.MODEL,
            f"Invalid state transition: {from_state.value} -> {to_state.value}",
            state=from_state,
        )
        self.from_state = from_state
        self.to_state = to_state


class StaleSnapshotError(OrchestratorError):
    """Снимок устарел (stale)."""
    def __init__(self, message: str = "Snapshot is stale"):
        super().__init__(ErrorCategory.STALE, message)


class FeedbackMismatchError(OrchestratorError):
    """ValidationFeedback не соответствует текущему снимку/хешу тестов."""
    def __init__(self, message: str):
        super().__init__(ErrorCategory.MODEL, message)


class BudgetExhaustedError(OrchestratorError):
    """Бюджет доработок исчерпан."""
    def __init__(self, message: str = "Improvement budget exhausted"):
        super().__init__(ErrorCategory.QUOTA, message)


@dataclass(frozen=True, slots=True)
class RunState:
    """
    Неизменяемое представление состояния прогона.
    Все поля обновляются атомарно через RunOrchestrator.
    """
    run_id: UUID
    execution_state: ExecutionState
    verdict: Verdict | None = None
    freshness: Freshness = Freshness.CURRENT
    merge_status: MergeStatus = MergeStatus.PENDING
    attempt_id: UUID | None = None
    snapshot_id: UUID | None = None
    test_bundle_hash: str | None = None
    source_sha: str | None = None
    target_sha: str | None = None
    improving_iteration: int = 0
    remaining_budget: int = MAX_IMPROVING_ITERATIONS
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def with_changes(self, **kwargs) -> RunState:
        """Создаёт новый RunState с изменениями (immutable pattern), не затирая поля при None."""
        def _pick(key: str, current_val):
            val = kwargs.get(key)
            return current_val if val is None else val

        return RunState(
            run_id=_pick("run_id", self.run_id),
            execution_state=_pick("execution_state", self.execution_state),
            verdict=_pick("verdict", self.verdict),
            freshness=_pick("freshness", self.freshness),
            merge_status=_pick("merge_status", self.merge_status),
            attempt_id=_pick("attempt_id", self.attempt_id),
            snapshot_id=_pick("snapshot_id", self.snapshot_id),
            test_bundle_hash=_pick("test_bundle_hash", self.test_bundle_hash),
            source_sha=_pick("source_sha", self.source_sha),
            target_sha=_pick("target_sha", self.target_sha),
            improving_iteration=_pick("improving_iteration", self.improving_iteration),
            remaining_budget=_pick("remaining_budget", self.remaining_budget),
            error_category=_pick("error_category", self.error_category),
            error_message=_pick("error_message", self.error_message),
            updated_at=datetime.now(timezone.utc),
        )


class RunOrchestrator:
    """
    Оркестратор прогонов: управляет переходами состояний, бюджетом доработок
    и валидацией обратной связи.

    Не хранит состояние между вызовами; работает с RunState как с данными.
    """

    def __init__(self):
        self._valid_transitions = VALID_TRANSITIONS

    # ==================== STATE TRANSITIONS ====================

    def transition(
        self,
        current: RunState,
        to_state: ExecutionState,
        *,
        verdict: Verdict | None = None,
        freshness: Freshness | None = None,
        merge_status: MergeStatus | None = None,
        attempt_id: UUID | None = None,
        snapshot_id: UUID | None = None,
        test_bundle_hash: str | None = None,
        source_sha: str | None = None,
        target_sha: str | None = None,
        improving_iteration: int | None = None,
        remaining_budget: int | None = None,
        error_category: ErrorCategory | None = None,
        error_message: str | None = None,
    ) -> RunState:
        """
        Выполняет переход состояния с валидацией.

        Args:
            current: Текущее состояние прогона.
            to_state: Целевое состояние выполнения.
            Остальные параметры: опциональные обновления связанных полей.

        Returns:
            Новый RunState после перехода.

        Raises:
            InvalidTransitionError: если переход недопустим.
        """
        if to_state not in self._valid_transitions.get(current.execution_state, set()):
            raise InvalidTransitionError(current.execution_state, to_state)

        # Специфичные проверки для определённых переходов
        if to_state == ExecutionState.IMPROVING:
            if current.improving_iteration >= MAX_IMPROVING_ITERATIONS:
                raise BudgetExhaustedError(
                    f"Max improving iterations ({MAX_IMPROVING_ITERATIONS}) reached"
                )
            if current.remaining_budget <= 0:
                raise BudgetExhaustedError("No remaining budget for improvement")

        if to_state == ExecutionState.COMPLETED and verdict is None:
            raise OrchestratorError(
                ErrorCategory.MODEL,
                "COMPLETED state requires a verdict",
                state=current.execution_state,
            )

        return current.with_changes(
            execution_state=to_state,
            verdict=verdict,
            freshness=freshness,
            merge_status=merge_status,
            attempt_id=attempt_id,
            snapshot_id=snapshot_id,
            test_bundle_hash=test_bundle_hash,
            source_sha=source_sha,
            target_sha=target_sha,
            improving_iteration=improving_iteration,
            remaining_budget=remaining_budget,
            error_category=error_category,
            error_message=error_message,
        )

    def start_preparing(self, run_id: UUID, attempt_id: UUID, snapshot_id: UUID) -> RunState:
        """Начало подготовки: queued -> preparing."""
        initial = RunState(
            run_id=run_id,
            execution_state=ExecutionState.QUEUED,
            attempt_id=attempt_id,
            snapshot_id=snapshot_id,
            remaining_budget=MAX_IMPROVING_ITERATIONS,
        )
        return self.transition(initial, ExecutionState.PREPARING)

    def start_generating(self, current: RunState, test_bundle_hash: str) -> RunState:
        """Начало генерации: preparing -> generating."""
        return self.transition(
            current,
            ExecutionState.GENERATING,
            test_bundle_hash=test_bundle_hash,
        )

    def start_validating(self, current: RunState) -> RunState:
        """Начало валидации: generating -> validating."""
        return self.transition(current, ExecutionState.VALIDATING)

    def start_running(self, current: RunState) -> RunState:
        """Запуск тестов: validating -> running."""
        return self.transition(current, ExecutionState.RUNNING)

    def start_checking_coverage(self, current: RunState) -> RunState:
        """Проверка покрытия: running -> checking_coverage."""
        return self.transition(current, ExecutionState.CHECKING_COVERAGE)

    def complete_with_verdict(
        self,
        current: RunState,
        verdict: Verdict,
        coverage_report: CoverageReportDTO,
    ) -> RunState:
        """
        Завершение прогона с вердиктом: checking_coverage -> completed.
        """
        if current.execution_state != ExecutionState.CHECKING_COVERAGE:
            raise InvalidTransitionError(current.execution_state, ExecutionState.COMPLETED)

        return self.transition(
            current,
            ExecutionState.COMPLETED,
            verdict=verdict,
        )

    def start_improving(
        self,
        current: RunState,
        feedback: ValidationFeedback,
    ) -> RunState:
        """
        Начало доработки: checking_coverage -> improving.
        Проверяет соответствие feedback текущему снимку и хешу тестов.
        """
        if current.execution_state != ExecutionState.CHECKING_COVERAGE:
            raise InvalidTransitionError(current.execution_state, ExecutionState.IMPROVING)

        # Критическая проверка: feedback должен относиться к ТЕКУЩЕМУ снимку и набору тестов
        if current.snapshot_id != feedback.snapshot_id:
            raise FeedbackMismatchError(
                f"Feedback snapshot_id {feedback.snapshot_id} != current {current.snapshot_id}"
            )
        if current.test_bundle_hash != feedback.test_bundle_hash:
            raise FeedbackMismatchError(
                f"Feedback test_bundle_hash {feedback.test_bundle_hash} != current {current.test_bundle_hash}"
            )

        next_iteration = current.improving_iteration + 1
        next_budget = current.remaining_budget - 1

        if next_iteration > MAX_IMPROVING_ITERATIONS:
            raise BudgetExhaustedError(
                f"Max improving iterations ({MAX_IMPROVING_ITERATIONS}) reached"
            )

        return self.transition(
            current,
            ExecutionState.IMPROVING,
            improving_iteration=next_iteration,
            remaining_budget=next_budget,
        )

    def fail(
        self,
        current: RunState,
        category: ErrorCategory,
        message: str,
    ) -> RunState:
        """Переход в failed из любого нетерминального состояния."""
        if current.execution_state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED):
            raise InvalidTransitionError(current.execution_state, ExecutionState.FAILED)

        return self.transition(
            current,
            ExecutionState.FAILED,
            verdict=Verdict.FAILED,
            error_category=category,
            error_message=message,
        )

    def cancel(self, current: RunState) -> RunState:
        """Отмена прогона: -> cancelled."""
        if current.execution_state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED):
            raise InvalidTransitionError(current.execution_state, ExecutionState.CANCELLED)

        return self.transition(current, ExecutionState.CANCELLED)

    def mark_stale(self, current: RunState) -> RunState:
        """Помечает снимок как устаревший (freshness = stale)."""
        return current.with_changes(freshness=Freshness.STALE)

    def update_merge_status(self, current: RunState, status: MergeStatus) -> RunState:
        """Обновляет статус мержа (отдельно от execution_state)."""
        return current.with_changes(merge_status=status)

    # ==================== VALIDATION FEEDBACK ====================

    def create_feedback(
        self,
        current: RunState,
        structured_errors: list[dict],
        uncovered_changed_lines: list[int],
    ) -> ValidationFeedback:
        """
        Создаёт ValidationFeedback для текущего состояния.

        Args:
            current: Текущее состояние прогона.
            structured_errors: Список ошибок с type, path, message.
            uncovered_changed_lines: Непокрытые изменённые строки.

        Returns:
            ValidationFeedback с правильными snapshot_id и test_bundle_hash.
        """
        if current.snapshot_id is None or current.test_bundle_hash is None:
            raise OrchestratorError(
                ErrorCategory.MODEL,
                "Cannot create feedback: missing snapshot_id or test_bundle_hash",
                state=current.execution_state,
            )

        return ValidationFeedback(
            snapshot_id=current.snapshot_id,
            test_bundle_hash=current.test_bundle_hash,
            structured_errors=structured_errors,
            uncovered_changed_lines=sorted(uncovered_changed_lines),
            iteration=current.improving_iteration + 1,
            remaining_budget=current.remaining_budget,
        )

    def validate_feedback(self, current: RunState, feedback: ValidationFeedback) -> None:
        """
        Валидирует входящий ValidationFeedback.

        Raises:
            FeedbackMismatchError: если feedback не соответствует текущему состоянию.
        """
        if current.snapshot_id != feedback.snapshot_id:
            raise FeedbackMismatchError(
                f"Feedback snapshot_id mismatch: expected {current.snapshot_id}, got {feedback.snapshot_id}"
            )
        if current.test_bundle_hash != feedback.test_bundle_hash:
            raise FeedbackMismatchError(
                f"Feedback test_bundle_hash mismatch: expected {current.test_bundle_hash}, got {feedback.test_bundle_hash}"
            )
        if feedback.iteration != current.improving_iteration + 1:
            raise FeedbackMismatchError(
                f"Feedback iteration {feedback.iteration} != expected {current.improving_iteration + 1}"
            )
        if feedback.remaining_budget != current.remaining_budget - 1:
            raise FeedbackMismatchError(
                f"Feedback remaining_budget {feedback.remaining_budget} != expected {current.remaining_budget - 1}"
            )

    # ==================== HELPERS ====================

    def is_terminal(self, state: ExecutionState) -> bool:
        """Проверяет, является ли состояние терминальным."""
        return state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED)

    def can_improve(self, current: RunState) -> bool:
        """Проверяет, возможна ли доработка (есть бюджет и не терминальное состояние)."""
        return (
            not self.is_terminal(current.execution_state)
            and current.improving_iteration < MAX_IMPROVING_ITERATIONS
            and current.remaining_budget > 0
        )

    def get_next_state_after_coverage(
        self,
        current: RunState,
        coverage_report: CoverageReportDTO,
    ) -> ExecutionState:
        """
        Определяет следующее состояние после проверки покрытия.

        Returns:
            IMPROVING если verdict=FAILED и есть бюджет, иначе COMPLETED.
        """
        if coverage_report.coverage_verdict == Verdict.FAILED and self.can_improve(current):
            return ExecutionState.IMPROVING
        return ExecutionState.COMPLETED


# ==================== T3 CALIBRATION CONSTANTS ====================

class T3Calibration:
    """
    Калибровка таймаутов и ретраев согласно ASSUMPTION T3.

    ВАЖНО: Не должно быть умножения retries между Celery, адаптером и SDK.
    Все таймауты и попытки управляются ЗДЕСЬ, на уровне оркестратора.
    """

    # Общий дедлайн на этап генерации (включая все ретраи)
    STAGE_DEADLINE_SECONDS = 120

    # Таймаут на один HTTP-запрос к LLM
    REQUEST_TIMEOUT_SECONDS = 60

    # Максимальное число транспортных попыток (сеть, 5xx, таймауты)
    MAX_TRANSPORT_ATTEMPTS = 3

    # Начальная генерация + доработки
    MAX_TOTAL_GENERATIONS = 1 + MAX_IMPROVING_ITERATIONS  # = 3

    @classmethod
    def get_llm_timeout_config(cls) -> dict:
        """Возвращает конфигурацию таймаутов для LLM-адаптера."""
        return {
            "stage_deadline_seconds": cls.STAGE_DEADLINE_SECONDS,
            "request_timeout_seconds": cls.REQUEST_TIMEOUT_SECONDS,
            "max_transport_attempts": cls.MAX_TRANSPORT_ATTEMPTS,
        }

    @classmethod
    def validate_no_retry_multiplication(cls, celery_retries: int, adapter_retries: int) -> None:
        """
        Проверяет, что нет умножения ретраев между слоями.

        Должно выполняться: celery_retries == 0 ИЛИ adapter_retries == 0
        (ретраи только на одном уровне).
        """
        if celery_retries > 0 and adapter_retries > 0:
            raise OrchestratorError(
                ErrorCategory.MODEL,
                f"Retry multiplication detected: celery_retries={celery_retries}, adapter_retries={adapter_retries}. "
                "Retries must be handled at exactly one layer.",
            )