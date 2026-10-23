from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.coverage.calculator import CoverageCalculator
from app.core.github_idempotency import (
    GitHubActionRepository,
    GitHubClient,
    GitHubIdempotencyManager,
    OperationContext,
)
from app.core.merge.gate import MergeDecisionBuilder, MergeGate
from app.core.orchestrator.orchestrator import (
    BudgetExhaustedError,
    FeedbackMismatchError,
    OrchestratorError,
    RunOrchestrator,
    T3Calibration,
)
from app.schemas import (
    CoverageReportDTO,
    ExecutionReport,
    ExecutionState,
    Freshness,
    GitHubActionRecord,
    GitHubOperationResult,
    GitHubOperationStatus,
    GitHubOperationType,
    MergeStatus,
    RunnerJob,
    RunnerMode,
    RunnerStatus,
    SourceSnapshot,
    SourceType,
    ValidationFeedback,
    Verdict,
)

VALID_HASH = "a" * 64
OTHER_HASH = "b" * 64
SOURCE_SHA = "1111111"
TARGET_SHA = "2222222"


# =====================================================================
# 1. Тесты контрактов T1 и правил безопасности (app.schemas)
# =====================================================================


def test_t1_schema_version_enforced():
    with pytest.raises(ValidationError):
        CoverageReportDTO(
            schema_version="T2",  # type: ignore[arg-type]
            attempt_id=uuid4(),
            run_id=uuid4(),
            covered_changed_lines=10,
            executable_changed_lines=10,
            coverage_verdict=Verdict.PASSED,
        )


def test_runner_job_rejects_unsafe_paths_and_commands():
    with pytest.raises(ValidationError):
        RunnerJob(
            job_id=uuid4(),
            run_id=uuid4(),
            snapshot_id=uuid4(),
            attempt_id=uuid4(),
            test_bundle_hash=VALID_HASH,
            artifacts=["../secret.env"],
            allowed_profile="python-default",
            cpu_limit=2,
            memory_limit_mb=512,
            timeout_seconds=60,
            pid_limit=50,
            disk_limit_mb=500,
            mode=RunnerMode.TEST,
        )

    with pytest.raises(ValidationError):
        RunnerJob(
            job_id=uuid4(),
            run_id=uuid4(),
            snapshot_id=uuid4(),
            attempt_id=uuid4(),
            test_bundle_hash=VALID_HASH,
            artifacts=["tests/test_app.py"],
            allowed_profile="python-default",
            cpu_limit=2,
            memory_limit_mb=512,
            timeout_seconds=60,
            pid_limit=50,
            disk_limit_mb=500,
            mode=RunnerMode.TEST,
            fixed_command="bash -c 'rm -rf /'",
        )


# =====================================================================
# 2. Тесты CoverageCalculator (Раздел 5: >= 90%, N/A при 0, пропажа файла)
# =====================================================================


def _make_snapshot(diff_text: str) -> SourceSnapshot:
    return SourceSnapshot(
        source_type=SourceType.GITHUB,
        run_id=uuid4(),
        snapshot_id=uuid4(),
        repo_owner="org",
        repo_name="demo",
        source_sha=SOURCE_SHA,
        target_sha=TARGET_SHA,
        diff_text=diff_text,
        content_reference="artifacts/snap",
        content_hash=VALID_HASH,
        environment_profile="py313",
    )


def _make_exec_report(
    snapshot: SourceSnapshot,
    report_paths: list[str],
    status: RunnerStatus = RunnerStatus.SUCCESS,
) -> ExecutionReport:
    return ExecutionReport(
        job_id=uuid4(),
        run_id=snapshot.run_id,
        snapshot_id=snapshot.snapshot_id,
        attempt_id=uuid4(),
        test_bundle_hash=VALID_HASH,
        image_digest="sha256:" + "0" * 57,
        status=status,
        exit_code=0 if status == RunnerStatus.SUCCESS else 1,
        collect_result={"collected": 5, "errors": 0},
        test_result={"passed": 5, "failed": 0},
        raw_coverage_reports=report_paths,
    )


def test_coverage_calculator_passes_at_90_percent_and_ignores_tests(tmp_path: Path):
    # Изменено 10 строк в src/service.py и 5 строк в tests/test_service.py (тесты не входят в знаменатель)
    diff_text = (
        "--- a/src/service.py\n"
        "+++ b/src/service.py\n"
        "@@ -1,0 +1,10 @@\n"
        + "".join(f"+x_{i} = {i}\n" for i in range(1, 11))
        + "--- a/tests/test_service.py\n"
        "+++ b/tests/test_service.py\n"
        "@@ -1,0 +1,5 @@\n"
        + "".join(f"+assert {i} == {i}\n" for i in range(1, 6))
    )
    src_file = tmp_path / "src" / "service.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("".join(f"x_{i} = {i}\n" for i in range(1, 11)))

    cov_json = tmp_path / "coverage.json"
    cov_json.write_text(
        json.dumps(
            {
                "files": {
                    "/workspace/src/service.py": {
                        "executed_lines": [1, 2, 3, 4, 5, 6, 7, 8, 9],
                    }
                }
            }
        )
    )

    snapshot = _make_snapshot(diff_text)
    exec_report = _make_exec_report(snapshot, [str(cov_json)])
    calc = CoverageCalculator(snapshot_root=tmp_path, container_workdir=Path("/workspace"))

    result = calc.calculate(exec_report, snapshot)
    assert result.executable_changed_lines == 10
    assert result.covered_changed_lines == 9
    assert result.coverage_verdict == Verdict.PASSED
    assert result.uncovered_changed_lines == [10]


def test_coverage_calculator_returns_not_applicable_when_zero_lines(tmp_path: Path):
    # Diff меняет только README.md -> 0 исполняемых Python-строк -> N/A, а не 100%
    diff_text = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # Title\n"
        "+Docs update\n"
    )
    cov_json = tmp_path / "coverage.json"
    cov_json.write_text(json.dumps({"files": {}}))

    snapshot = _make_snapshot(diff_text)
    exec_report = _make_exec_report(snapshot, [str(cov_json)])
    calc = CoverageCalculator(snapshot_root=tmp_path, container_workdir=Path("/workspace"))

    result = calc.calculate(exec_report, snapshot)
    assert result.executable_changed_lines == 0
    assert result.coverage_percentage is None
    assert result.coverage_verdict == Verdict.NOT_APPLICABLE


# =====================================================================
# 3. Тесты RunOrchestrator (Переходы, бюджет T3, защита Feedback)
# =====================================================================


def test_orchestrator_full_lifecycle_and_budget_limit():
    orch = RunOrchestrator()
    run_id, attempt_id, snapshot_id = uuid4(), uuid4(), uuid4()

    state = orch.start_preparing(run_id, attempt_id, snapshot_id)
    assert state.execution_state == ExecutionState.PREPARING

    state = orch.start_generating(state, VALID_HASH)
    state = orch.start_validating(state)
    state = orch.start_running(state)
    state = orch.start_checking_coverage(state)

    # Итерация доработки #1
    fb1 = orch.create_feedback(state, [], [10])
    state = orch.start_improving(state, fb1)
    assert state.improving_iteration == 1
    assert state.remaining_budget == 1

    # Возврат в цикл после 1-й доработки
    state = orch.start_validating(state)
    state = orch.start_running(state)
    state = orch.start_checking_coverage(state)

    # Итерация доработки #2 (исчерпание бюджета T3 = 2 доработки)
    fb2 = orch.create_feedback(state, [], [10])
    state = orch.start_improving(state, fb2)
    assert state.improving_iteration == 2
    assert state.remaining_budget == 0
    assert not orch.can_improve(state)

    # Попытка 3-й доработки (бюджет исчерпан) должна выбрасывать BudgetExhaustedError
    state = orch.start_validating(state)
    state = orch.start_running(state)
    state = orch.start_checking_coverage(state)
    fb3 = orch.create_feedback(state, [], [10])
    with pytest.raises((BudgetExhaustedError, OrchestratorError)):
        orch.start_improving(state, fb3)


def test_orchestrator_rejects_foreign_feedback():
    orch = RunOrchestrator()
    state = orch.start_preparing(uuid4(), uuid4(), uuid4())
    state = orch.start_generating(state, VALID_HASH)
    state = orch.start_validating(state)

    foreign_feedback = ValidationFeedback(
        snapshot_id=uuid4(),  # чужой snapshot_id
        test_bundle_hash=OTHER_HASH,
        structured_errors=[],
        uncovered_changed_lines=[1, 2],
        iteration=1,
        remaining_budget=1,
    )
    with pytest.raises(FeedbackMismatchError):
        orch.validate_feedback(state, foreign_feedback)


def test_t3_no_retry_multiplication():
    with pytest.raises(OrchestratorError):
        T3Calibration.validate_no_retry_multiplication(celery_retries=2, adapter_retries=3)


# =====================================================================
# 4. Тесты MergeGate (Порог 90%, блокировка N/A и stale, автомерж)
# =====================================================================


def _make_cov_dto(run_id: UUID, attempt_id: UUID, covered: int, executable: int, verdict: Verdict) -> CoverageReportDTO:
    pct = (covered / executable * 100.0) if executable > 0 else None
    return CoverageReportDTO(
        attempt_id=attempt_id,
        run_id=run_id,
        covered_changed_lines=covered,
        executable_changed_lines=executable,
        coverage_percentage=pct,
        coverage_verdict=verdict,
    )


def test_merge_gate_exact_integer_threshold_and_na_block():
    gate = MergeGate()
    builder = MergeDecisionBuilder(gate)
    run_id, attempt_id, user_id = uuid4(), uuid4(), uuid4()

    # 1) 89 из 100 -> ЗАПРЕЩЕНО (даже при ExecutionState.COMPLETED)
    out_89 = builder.build_from_run_data(
        run_id=run_id,
        attempt_id=attempt_id,
        execution_state=ExecutionState.COMPLETED,
        verdict=Verdict.PASSED,
        freshness=Freshness.CURRENT,
        merge_status=MergeStatus.PENDING,
        coverage_report=_make_cov_dto(run_id, attempt_id, 89, 100, Verdict.FAILED),
        test_bundle_hash=VALID_HASH,
        source_sha=SOURCE_SHA,
        target_sha=TARGET_SHA,
        owner_user_id=user_id,
        owner_github_login="aruchimo",
        auto_merge_enabled=False,
        target_branch_protected=True,
        user_has_write_permission=True,
        github_app_has_write_permission=True,
    )
    assert not out_89.allowed

    # 2) 90 из 100 -> РАЗРЕШЕНО
    out_90 = builder.build_from_run_data(
        run_id=run_id,
        attempt_id=attempt_id,
        execution_state=ExecutionState.COMPLETED,
        verdict=Verdict.PASSED,
        freshness=Freshness.CURRENT,
        merge_status=MergeStatus.PENDING,
        coverage_report=_make_cov_dto(run_id, attempt_id, 90, 100, Verdict.PASSED),
        test_bundle_hash=VALID_HASH,
        source_sha=SOURCE_SHA,
        target_sha=TARGET_SHA,
        owner_user_id=user_id,
        owner_github_login="aruchimo",
        auto_merge_enabled=False,
        target_branch_protected=False,
        user_has_write_permission=True,
        github_app_has_write_permission=True,
    )
    assert out_90.allowed
    assert out_90.merge_decision.allowed

    # 3) 0 измеряемых строк (NOT_APPLICABLE / N/A) -> ЗАПРЕЩЕНО (согласно T4)
    out_na = builder.build_from_run_data(
        run_id=run_id,
        attempt_id=attempt_id,
        execution_state=ExecutionState.COMPLETED,
        verdict=Verdict.NOT_APPLICABLE,
        freshness=Freshness.CURRENT,
        merge_status=MergeStatus.PENDING,
        coverage_report=_make_cov_dto(run_id, attempt_id, 0, 0, Verdict.NOT_APPLICABLE),
        test_bundle_hash=VALID_HASH,
        source_sha=SOURCE_SHA,
        target_sha=TARGET_SHA,
        owner_user_id=user_id,
        owner_github_login="aruchimo",
        auto_merge_enabled=False,
        target_branch_protected=True,
        user_has_write_permission=True,
        github_app_has_write_permission=True,
    )
    assert not out_na.allowed

    # 4) Автомерж без защищённой target-ветки -> ЗАПРЕЩЕНО (правило 9)
    out_auto_unprotected = builder.build_from_run_data(
        run_id=run_id,
        attempt_id=attempt_id,
        execution_state=ExecutionState.COMPLETED,
        verdict=Verdict.PASSED,
        freshness=Freshness.CURRENT,
        merge_status=MergeStatus.PENDING,
        coverage_report=_make_cov_dto(run_id, attempt_id, 95, 100, Verdict.PASSED),
        test_bundle_hash=VALID_HASH,
        source_sha=SOURCE_SHA,
        target_sha=TARGET_SHA,
        owner_user_id=user_id,
        owner_github_login="aruchimo",
        auto_merge_enabled=True,
        target_branch_protected=False,
        user_has_write_permission=True,
        github_app_has_write_permission=True,
    )
    assert not out_auto_unprotected.allowed


# =====================================================================
# 5. Тесты GitHubIdempotencyManager (Защита от дублей, stale и unknown)
# =====================================================================


class InMemoryActionRepo(GitHubActionRepository):
    def __init__(self) -> None:
        self.by_key: dict[str, GitHubActionRecord] = {}
        self.by_rec_id: dict[UUID, GitHubActionRecord] = {}

    async def create(self, action: GitHubActionRecord) -> None:
        key = f"{action.run_id}:{action.operation_type.value}:{action.expected_sha}"
        self.by_key[key] = action
        self.by_rec_id[action.reconciliation_id] = action

    async def update(self, action: GitHubActionRecord) -> None:
        self.by_rec_id[action.reconciliation_id] = action
        for k, v in list(self.by_key.items()):
            if v.reconciliation_id == action.reconciliation_id:
                self.by_key[k] = action

    async def find_by_idempotency_key(self, key: str) -> GitHubActionRecord | None:
        return self.by_key.get(key)

    async def find_by_reconciliation_id(self, reconciliation_id: UUID) -> GitHubActionRecord | None:
        return self.by_rec_id.get(reconciliation_id)

    async def find_by_run_and_operation(
        self, run_id: UUID, operation_type: GitHubOperationType
    ) -> list[GitHubActionRecord]:
        return [
            a
            for a in self.by_rec_id.values()
            if a.run_id == run_id and a.operation_type == operation_type
        ]


class FakeGitHubClient(GitHubClient):
    def __init__(self, branch_sha: str = SOURCE_SHA, pr_head_sha: str = SOURCE_SHA) -> None:
        self.branch_sha = branch_sha
        self.pr_head_sha = pr_head_sha

    async def get_branch_sha(self, repo_owner: str, repo_name: str, branch_name: str) -> str | None:
        if branch_name == "main":
            return TARGET_SHA
        return self.branch_sha

    async def get_pr_head_sha(self, repo_owner: str, repo_name: str, pr_number: int) -> str | None:
        return self.pr_head_sha

    async def reconcile_operation(self, action: GitHubActionRecord) -> GitHubOperationResult:
        return GitHubOperationResult(
            operation_type=action.operation_type,
            repo_owner=action.repo_owner,
            repo_name=action.repo_name,
            branch_name=action.branch_name,
            pr_number=action.pr_number,
            expected_sha=action.expected_sha,
            actual_sha=action.expected_sha,
            status=GitHubOperationStatus.SUCCESS,
            safe_reason="Verified in GitHub after unknown state",
            reconciliation_id=action.reconciliation_id,
        )


def test_github_idempotency_blocks_stale_pr_head():
    repo = InMemoryActionRepo()
    # В GitHub появился новый коммит ("9999999" != SOURCE_SHA)
    client = FakeGitHubClient(branch_sha=SOURCE_SHA, pr_head_sha="9999999")
    mgr = GitHubIdempotencyManager(repo, client)

    run_id = uuid4()
    ctx = OperationContext(
        operation_type=GitHubOperationType.MERGE_PR,
        owner_user_id=uuid4(),
        owner_github_login="aruchimo",
        run_id=run_id,
        attempt_id=uuid4(),
        repo_owner="org",
        repo_name="demo",
        branch_name=f"ai-tests/{run_id}",
        expected_source_sha=SOURCE_SHA,
        expected_target_sha=TARGET_SHA,
        expected_head_sha=SOURCE_SHA,
        pr_number=42,
    )

    res = asyncio.run(mgr.prepare_operation(ctx))
    assert not res.can_proceed
    assert res.reason is not None and "stale" in res.reason.lower()
