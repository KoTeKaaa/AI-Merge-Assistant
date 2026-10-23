from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.core.merge.gate import MergeDecisionBuilder, MergeGate
from app.core.security import (
    generate_csrf_token,
    generate_oauth_state,
    verify_github_webhook_signature,
)
from app.schemas import (
    SCHEMA_VERSION,
    CoverageReportDTO,
    ErrorCategory,
    ExecutionState,
    Freshness,
    MergeStatus,
    Mode,
    RequirementTag,
    ScenarioItem,
    ScenarioSet,
    SourceType,
    TestBundle,
    TestItem,
    Verdict,
)

api_router = APIRouter()


class APIErrorException(Exception):
    """Единое исключение API, возвращающее строго типизированный JSON по i0-api-spec.md."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        category: ErrorCategory,
        safe_reason: str,
        run_id: UUID | None = None,
        attempt_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "error_code": error_code,
            "message": message,
            "category": category.value,
            "safe_reason": safe_reason,
            "details": details or {},
        }
        if run_id is not None:
            self.payload["run_id"] = str(run_id)
        if attempt_id is not None:
            self.payload["attempt_id"] = str(attempt_id)


@dataclass
class InMemoryStore:
    """In-memory хранилище для автономной проверки контрактов I0/I1 и OpenAPI."""

    oauth_states: set[str] = field(default_factory=set)
    sessions: dict[str, UUID] = field(default_factory=dict)
    users: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    installations: dict[str, UUID] = field(default_factory=dict)
    runs: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    uploads: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    webhook_deliveries: dict[str, bool] = field(default_factory=dict)

    def reset_with_demo_user(self) -> tuple[str, UUID, UUID]:
        self.oauth_states.clear()
        self.sessions.clear()
        self.users.clear()
        self.installations.clear()
        self.runs.clear()
        self.uploads.clear()
        self.webhook_deliveries.clear()

        user_id = UUID("11111111-1111-1111-1111-111111111111")
        other_user_id = UUID("22222222-2222-2222-2222-222222222222")
        token = "demo-session-token"
        self.sessions[token] = user_id
        self.sessions["other-session-token"] = other_user_id
        self.users[user_id] = {
            "id": user_id,
            "github_login": "aruchimo",
            "github_id": "10001",
            "installations": ["inst-1"],
        }
        self.users[other_user_id] = {
            "id": other_user_id,
            "github_login": "other-user",
            "github_id": "10002",
            "installations": ["inst-2"],
        }
        self.installations["inst-1"] = user_id
        self.installations["inst-2"] = other_user_id
        return token, user_id, other_user_id


store = InMemoryStore()
store.reset_with_demo_user()


def get_current_user_id(
    session_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> UUID:
    token = session_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    if not token or token not in store.sessions:
        raise APIErrorException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code="unauthorized",
            message="Authentication required.",
            category=ErrorCategory.RIGHTS,
            safe_reason="Please sign in via GitHub to continue.",
        )
    return store.sessions[token]


def _ForbiddenNoLeak() -> APIErrorException:
    """Формат ошибки 403 без утечки чужих run_id, repo_name или user_id."""
    return APIErrorException(
        status_code=status.HTTP_403_FORBIDDEN,
        error_code="forbidden_resource",
        message="Access denied.",
        category=ErrorCategory.RIGHTS,
        safe_reason="You do not have permission to access this resource.",
        details={},
    )


def _get_owned_run(run_id: UUID, user_id: UUID, check_stale: bool = False) -> dict[str, Any]:
    run = store.runs.get(run_id)
    if run is None:
        raise APIErrorException(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="run_not_found",
            message="Resource not found.",
            category=ErrorCategory.RIGHTS,
            safe_reason="Requested resource was not found.",
        )
    if run["user_id"] != user_id:
        raise _ForbiddenNoLeak()
    if check_stale and run["freshness"] == Freshness.STALE:
        raise APIErrorException(
            status_code=status.HTTP_409_CONFLICT,
            error_code="stale_snapshot",
            message="Target SHA изменился с момента создания прогона.",
            category=ErrorCategory.STALE,
            run_id=run_id,
            attempt_id=run["attempt_id"],
            details={
                "expected_target_sha": run["target_sha"],
                "actual_target_sha": run.get("actual_target_sha", "moved_sha"),
            },
            safe_reason="The target branch has moved. Please refresh the snapshot and retry.",
        )
    return run


# =====================================================================
# 1. AUTH (/auth/*)
# =====================================================================


@api_router.get("/auth/github/login", status_code=status.HTTP_302_FOUND)
def github_login() -> Response:
    state = generate_oauth_state()
    store.oauth_states.add(state)
    redirect_url = f"https://github.com/login/oauth/authorize?state={state}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@api_router.get("/auth/github/callback")
def github_callback(response: Response, state: str = Query(...), code: str = Query("demo")) -> dict[str, Any]:
    if not state:
        raise APIErrorException(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="invalid_oauth_state",
            message="Missing state parameter.",
            category=ErrorCategory.RIGHTS,
            safe_reason="Invalid OAuth state.",
        )
    if state not in store.oauth_states:
        raise APIErrorException(
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="forbidden_resource",
            message="OAuth state expired or already used.",
            category=ErrorCategory.RIGHTS,
            safe_reason="OAuth state is invalid or expired.",
        )
    store.oauth_states.remove(state)
    session_token = f"sess-{uuid4()}"
    user_id = UUID("11111111-1111-1111-1111-111111111111")
    store.sessions[session_token] = user_id
    csrf = generate_csrf_token()
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "authenticated": True,
        "csrf_token": csrf,
        "code_exchanged": bool(code),
    }


@api_router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None),
    user_id: UUID = Depends(get_current_user_id),
) -> Response:
    _ = user_id
    if session_token:
        store.sessions.pop(session_token, None)
    response.delete_cookie("session_token")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.get("/auth/me")
def get_me(user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    user = store.users[user_id]
    return {
        "schema_version": SCHEMA_VERSION,
        "user_id": str(user["id"]),
        "github_login": user["github_login"],
        "installations": user["installations"],
    }


# =====================================================================
# 2. INSTALLATIONS & REPOS (/installations, /repos)
# =====================================================================


@api_router.get("/installations")
def list_installations(user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    owned = [inst_id for inst_id, owner in store.installations.items() if owner == user_id]
    return {"schema_version": SCHEMA_VERSION, "installations": owned}


@api_router.post("/installations/{installation_id}/connect")
def connect_installation(
    installation_id: str,
    user_id: UUID = Depends(get_current_user_id),
) -> dict[str, Any]:
    owner = store.installations.get(installation_id)
    if owner is None:
        raise APIErrorException(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="installation_not_found",
            message="Installation not found.",
            category=ErrorCategory.RIGHTS,
            safe_reason="GitHub App installation was not found.",
        )
    if owner != user_id:
        raise _ForbiddenNoLeak()
    return {"schema_version": SCHEMA_VERSION, "installation_id": installation_id, "connected": True}


@api_router.get("/repos")
def list_repos(user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    _ = user_id
    return {
        "schema_version": SCHEMA_VERSION,
        "repositories": [
            {
                "owner": "aruchimo",
                "name": "demo-python-repo",
                "is_public": True,
                "can_analyze": True,
                "can_write": True,
            }
        ],
    }


@api_router.get("/repos/{owner}/{repo}/branches")
def list_branches(
    owner: str,
    repo: str,
    user_id: UUID = Depends(get_current_user_id),
) -> dict[str, Any]:
    if owner != store.users[user_id]["github_login"]:
        raise _ForbiddenNoLeak()
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": f"{owner}/{repo}",
        "branches": [
            {"name": "main", "sha": "2222222", "protected": True},
            {"name": "feature/login", "sha": "1111111", "protected": False},
        ],
    }


@api_router.get("/repos/{owner}/{repo}/branches/{branch}/sha")
def get_branch_sha(
    owner: str,
    repo: str,
    branch: str,
    user_id: UUID = Depends(get_current_user_id),
) -> dict[str, Any]:
    if owner != store.users[user_id]["github_login"]:
        raise _ForbiddenNoLeak()
    sha = "2222222" if branch == "main" else "1111111"
    return {"schema_version": SCHEMA_VERSION, "repo": f"{owner}/{repo}", "branch": branch, "sha": sha}


# =====================================================================
# 3. RUNS (/runs/*)
# =====================================================================


class CreateRunRequest(BaseModel):
    source_type: SourceType = SourceType.GITHUB
    repo_owner: str = "aruchimo"
    repo_name: str = "demo-python-repo"
    source_sha: str = Field(default="1111111", pattern=r"^[a-f0-9]{7,40}$")
    target_sha: str = Field(default="2222222", pattern=r"^[a-f0-9]{7,40}$")
    mode: Literal["default", "lazy"] = "default"
    description: str | None = "Add login handler"
    environment_profile: str = "py313"
    dependency_profile: str = "pytest-default"
    auto_merge_enabled: bool = False


@api_router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(
    payload: CreateRunRequest,
    user_id: UUID = Depends(get_current_user_id),
) -> dict[str, Any]:
    if payload.source_type == SourceType.GITHUB and payload.repo_owner != store.users[user_id]["github_login"]:
        raise _ForbiddenNoLeak()
    if payload.mode == "default" and not payload.description:
        raise APIErrorException(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="missing_description",
            message="Default mode requires a change description.",
            category=ErrorCategory.MODEL,
            safe_reason="Please provide a description for default mode or switch to lazy mode.",
        )

    run_id = uuid4()
    attempt_id = uuid4()
    snapshot_id = uuid4()
    bundle_hash = "a" * 64

    cov = CoverageReportDTO(
        attempt_id=attempt_id,
        run_id=run_id,
        covered_changed_lines=19,
        executable_changed_lines=20,
        coverage_percentage=95.0,
        covered_files=["src/service.py"],
        uncovered_changed_lines=[20],
        coverage_verdict=Verdict.PASSED,
    )

    store.runs[run_id] = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "snapshot_id": snapshot_id,
        "user_id": user_id,
        "source_type": payload.source_type,
        "repo_owner": payload.repo_owner,
        "repo_name": payload.repo_name,
        "source_sha": payload.source_sha,
        "target_sha": payload.target_sha,
        "execution_state": ExecutionState.QUEUED,
        "verdict": Verdict.PASSED,
        "freshness": Freshness.CURRENT,
        "merge_status": MergeStatus.PENDING,
        "mode": payload.mode,
        "auto_merge_enabled": payload.auto_merge_enabled,
        "test_bundle_hash": bundle_hash,
        "coverage_report": cov,
        "is_fake_offline": True,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "status": "accepted",
        "execution_state": ExecutionState.QUEUED.value,
        "message": f"Run registered. Poll /runs/{run_id}/status.",
        "poll_interval_seconds": 5,
    }


@api_router.get("/runs/{run_id}")
def get_run(run_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    run = _get_owned_run(run_id, user_id, check_stale=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run["run_id"]),
        "attempt_id": str(run["attempt_id"]),
        "execution_state": run["execution_state"].value,
        "verdict": run["verdict"].value if run["verdict"] else None,
        "freshness": run["freshness"].value,
        "merge_status": run["merge_status"].value,
        "mode": run["mode"],
        "is_fake_offline": run["is_fake_offline"],
    }


@api_router.get("/runs/{run_id}/status")
def get_run_status(run_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    run = _get_owned_run(run_id, user_id, check_stale=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run["run_id"]),
        "execution_state": run["execution_state"].value,
        "verdict": run["verdict"].value if run["verdict"] else None,
        "freshness": run["freshness"].value,
        "merge_status": run["merge_status"].value,
    }


@api_router.get("/runs/{run_id}/scenarios")
def get_run_scenarios(run_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> ScenarioSet:
    run = _get_owned_run(run_id, user_id)
    tag = RequirementTag.INFERRED if run["mode"] == "lazy" else RequirementTag.REQUIREMENTS
    return ScenarioSet(
        set_id=uuid4(),
        run_id=run_id,
        snapshot_id=run["snapshot_id"],
        scenarios=[
            ScenarioItem(
                scenario_id=uuid4(),
                source_refs=[run["snapshot_id"]],
                requirement_tag=tag,
                gherkin_text="Feature: Demo\n  Scenario: Check change\n    Given input\n    Then output is valid",
            )
        ],
        prompt_model="gemini-2.5-flash-fake-offline",
        prompt_version="v1",
        usage="unknown",
        set_hash="c" * 64,
    )


@api_router.get("/runs/{run_id}/tests")
def get_run_tests(run_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> TestBundle:
    run = _get_owned_run(run_id, user_id)
    return TestBundle(
        bundle_id=uuid4(),
        run_id=run_id,
        snapshot_id=run["snapshot_id"],
        tests=[
            TestItem(
                test_path="tests/test_generated.py",
                test_text="def test_generated():\n    assert True\n",
                linked_scenario_ids=[],
            )
        ],
        bundle_hash=run["test_bundle_hash"],
        prompt_model="gemini-2.5-flash-fake-offline",
        prompt_version="v1",
        usage="unknown",
    )


@api_router.get("/runs/{run_id}/coverage")
def get_run_coverage(run_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> CoverageReportDTO:
    run = _get_owned_run(run_id, user_id)
    return run["coverage_report"]


# =====================================================================
# 4. UPLOADS, EXPORTS, MERGE, STATS, WEBHOOKS
# =====================================================================


class UploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    page_count: int = Field(default=5, ge=1, le=500)
    size_bytes: int = Field(default=1024, ge=1)


@api_router.post("/uploads", status_code=status.HTTP_202_ACCEPTED)
def create_upload(
    payload: UploadRequest,
    user_id: UUID = Depends(get_current_user_id),
) -> dict[str, Any]:
    if payload.size_bytes > 10 * 1024 * 1024:
        raise APIErrorException(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="file_too_large",
            message="File exceeds 10 MB limit.",
            category=ErrorCategory.ENVIRONMENT,
            safe_reason="Uploaded file exceeds the maximum allowed size (10 MB).",
        )
    upload_id = uuid4()
    background_queued = payload.page_count > 50
    store.uploads[upload_id] = {
        "upload_id": upload_id,
        "user_id": user_id,
        "filename": payload.filename,
        "page_count": payload.page_count,
        "background_queued": background_queued,
        "status": "processing_in_celery" if background_queued else "ready",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "upload_id": str(upload_id),
        "status": store.uploads[upload_id]["status"],
        "background_queued": background_queued,
    }


@api_router.get("/uploads/{upload_id}")
def get_upload(upload_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    up = store.uploads.get(upload_id)
    if up is None:
        raise APIErrorException(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="upload_not_found",
            message="Upload not found.",
            category=ErrorCategory.RIGHTS,
            safe_reason="Requested upload was not found.",
        )
    if up["user_id"] != user_id:
        raise _ForbiddenNoLeak()
    return {
        "schema_version": SCHEMA_VERSION,
        "upload_id": str(upload_id),
        "status": up["status"],
        "background_queued": up["background_queued"],
    }


@api_router.get("/runs/{run_id}/export")
def export_run_artifacts(
    run_id: UUID,
    format: str = Query("zip"),
    user_id: UUID = Depends(get_current_user_id),
) -> dict[str, Any]:
    if format not in {"feature", "py", "zip"}:
        raise APIErrorException(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="unsupported_export_format",
            message="Only .feature, .py, and zip formats are supported (.js is forbidden).",
            category=ErrorCategory.RIGHTS,
            safe_reason="Unsupported export format requested.",
        )
    run = _get_owned_run(run_id, user_id, check_stale=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run["run_id"]),
        "format": format,
        "files": ["scenarios/generated.feature", "tests/test_generated.py"],
    }


class MergeRequest(BaseModel):
    mode: Literal["manual", "auto"] = "manual"


@api_router.post("/runs/{run_id}/merge")
def trigger_merge(
    run_id: UUID,
    payload: MergeRequest,
    user_id: UUID = Depends(get_current_user_id),
) -> dict[str, Any]:
    run = _get_owned_run(run_id, user_id, check_stale=True)

    # Идемпотентность: повторный запрос на уже выполненный мерж не создаёт дубликат
    if run["merge_status"] == MergeStatus.COMPLETED:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": str(run_id),
            "merge_status": MergeStatus.COMPLETED.value,
            "idempotent_replay": True,
            "decision": run["last_merge_decision"].model_dump(mode="json"),
        }

    gate = MergeGate()
    builder = MergeDecisionBuilder(gate)
    out = builder.build_from_run_data(
        run_id=run["run_id"],
        attempt_id=run["attempt_id"],
        execution_state=run["execution_state"],
        verdict=run["verdict"],
        freshness=run["freshness"],
        merge_status=run["merge_status"],
        coverage_report=run["coverage_report"],
        test_bundle_hash=run["test_bundle_hash"],
        source_sha=run["source_sha"],
        target_sha=run["target_sha"],
        owner_user_id=user_id,
        owner_github_login=store.users[user_id]["github_login"],
        auto_merge_enabled=(payload.mode == "auto" and run["auto_merge_enabled"]),
        target_branch_protected=True,
        user_has_write_permission=True,
        github_app_has_write_permission=True,
    )

    if not out.allowed:
        run["merge_status"] = MergeStatus.DENIED
        raise APIErrorException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="merge_conditions_not_met",
            message="; ".join(out.denial_reasons),
            category=ErrorCategory.TESTS,
            run_id=run_id,
            attempt_id=run["attempt_id"],
            safe_reason="Merge gate conditions are not satisfied.",
            details={"denial_reasons": out.denial_reasons},
        )

    run["merge_status"] = MergeStatus.COMPLETED
    run["last_merge_decision"] = out.merge_decision
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "merge_status": run["merge_status"].value,
        "idempotent_replay": False,
        "decision": out.merge_decision.model_dump(mode="json"),
    }


@api_router.get("/runs/{run_id}/merge/status")
def get_merge_status(run_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    run = _get_owned_run(run_id, user_id, check_stale=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "merge_status": run["merge_status"].value,
    }


@api_router.get("/stats")
def get_stats(user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    user_runs = [r for r in store.runs.values() if r["user_id"] == user_id]
    return {
        "schema_version": SCHEMA_VERSION,
        "total_runs": len(user_runs),
        "completed_runs": sum(1 for r in user_runs if r["execution_state"] == ExecutionState.COMPLETED),
        "disclaimer": (
            "Покрытие показывает, какая доля кода выполнялась при тестировании. "
            "Высокое покрытие и успешные тесты не гарантируют отсутствие ошибок; "
            "низкое покрытие само по себе не означает наличие ошибки."
        ),
    }


@api_router.get("/stats/runs")
def get_stats_runs(user_id: UUID = Depends(get_current_user_id)) -> dict[str, Any]:
    user_runs = [str(r["run_id"]) for r in store.runs.values() if r["user_id"] == user_id]
    return {"schema_version": SCHEMA_VERSION, "run_ids": user_runs}


@api_router.post("/webhooks/github", status_code=status.HTTP_204_NO_CONTENT)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> Response:
    # Правило 6: HMAC-SHA256 проверяется по сырым байтам ДО любого парсинга JSON
    raw_body = await request.body()
    is_valid, _ = verify_github_webhook_signature(raw_body, x_hub_signature_256)
    if not is_valid:
        raise APIErrorException(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="invalid_webhook_signature",
            message="Webhook HMAC-SHA256 signature verification failed.",
            category=ErrorCategory.RIGHTS,
            safe_reason="Invalid webhook signature.",
        )

    if not x_github_delivery or not x_github_event:
        raise APIErrorException(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="missing_webhook_headers",
            message="Missing X-GitHub-Delivery or X-GitHub-Event header.",
            category=ErrorCategory.RIGHTS,
            safe_reason="Missing required GitHub webhook headers.",
        )

    if x_github_event not in {"push", "pull_request", "installation", "installation_repositories"}:
        raise APIErrorException(
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="unsupported_webhook_event",
            message=f"Unsupported event: {x_github_event}",
            category=ErrorCategory.RIGHTS,
            safe_reason="Webhook event is not supported.",
        )

    if store.webhook_deliveries.get(x_github_delivery):
        raise APIErrorException(
            status_code=status.HTTP_409_CONFLICT,
            error_code="duplicate_webhook_delivery",
            message="Webhook delivery_id already processed.",
            category=ErrorCategory.STALE,
            safe_reason="Duplicate webhook delivery.",
        )

    store.webhook_deliveries[x_github_delivery] = True
    return Response(status_code=status.HTTP_204_NO_CONTENT)
