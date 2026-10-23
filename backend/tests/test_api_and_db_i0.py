from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

from fastapi.testclient import TestClient

from app.api.router import store
from app.core.config import settings
from app.core.security import decrypt_token, encrypt_token
from app.db.base import Base
from app.db import models  # noqa: F401
from app.main import app
from app.schemas import ExecutionState, Freshness

client = TestClient(app)


def test_sqlalchemy_metadata_has_all_12_tables():
    expected_tables = {
        "users",
        "sessions",
        "github_connections",
        "installations",
        "repositories",
        "runs",
        "attempts",
        "scenarios",
        "artifacts",
        "coverage_reports",
        "github_actions",
        "webhook_deliveries",
    }
    assert expected_tables.issubset(set(Base.metadata.tables.keys()))


def test_token_encryption_roundtrip():
    raw = "ghu_secret_github_user_token_12345"
    enc = encrypt_token(raw)
    assert raw not in enc
    assert decrypt_token(enc) == raw


def test_unauthorized_request_returns_401():
    store.reset_with_demo_user()
    resp = client.get("/runs/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 401
    body = resp.json()
    assert body["schema_version"] == "T1"
    assert body["error_code"] == "unauthorized"


def test_create_run_returns_202_and_blocks_foreign_user_without_leak():
    token, _, _ = store.reset_with_demo_user()
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/runs",
        headers=headers,
        json={
            "source_type": "github",
            "repo_owner": "aruchimo",
            "repo_name": "demo-python-repo",
            "source_sha": "1111111",
            "target_sha": "2222222",
            "mode": "default",
            "description": "Add feature",
        },
    )
    assert create_resp.status_code == 202
    run_id = create_resp.json()["run_id"]

    # Чужой пользователь запрашивает чужой run_id -> 403 без утечки run_id в ответе
    foreign_resp = client.get(
        f"/runs/{run_id}",
        headers={"Authorization": "Bearer other-session-token"},
    )
    assert foreign_resp.status_code == 403
    foreign_body = foreign_resp.json()
    assert "run_id" not in foreign_body
    assert run_id not in str(foreign_body)


def test_stale_run_returns_409_and_js_export_is_rejected():
    token, _, _ = store.reset_with_demo_user()
    headers = {"Authorization": f"Bearer {token}"}

    run_id = client.post("/runs", headers=headers, json={}).json()["run_id"]

    # Запрет экспорта .js (разрешены только .feature, .py, zip)
    js_resp = client.get(f"/runs/{run_id}/export?format=js", headers=headers)
    assert js_resp.status_code == 400

    # Переводим снимок в STALE -> ожидаем 409 Conflict
    store.runs[UUID(run_id)]["freshness"] = Freshness.STALE
    stale_resp = client.get(f"/runs/{run_id}/status", headers=headers)
    assert stale_resp.status_code == 409
    assert stale_resp.json()["error_code"] == "stale_snapshot"


def test_merge_idempotency_does_not_duplicate():
    token, _, _ = store.reset_with_demo_user()
    headers = {"Authorization": f"Bearer {token}"}

    run_id = client.post("/runs", headers=headers, json={}).json()["run_id"]
    store.runs[UUID(run_id)]["execution_state"] = ExecutionState.COMPLETED

    first = client.post(f"/runs/{run_id}/merge", headers=headers, json={"mode": "manual"})
    assert first.status_code == 200
    assert first.json()["idempotent_replay"] is False

    # Повторное нажатие кнопки мержа возвращает идемпотентный ответ без второго PR/мержа
    second = client.post(f"/runs/{run_id}/merge", headers=headers, json={"mode": "manual"})
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True


def test_webhook_hmac_verification_and_deduplication():
    store.reset_with_demo_user()
    raw_payload = b'{"ref": "refs/heads/main"}'
    valid_sig = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode("utf-8"),
        raw_payload,
        hashlib.sha256,
    ).hexdigest()

    # 1. Неверная подпись отвергается до парсинга и не записывает delivery_id
    bad_resp = client.post(
        "/webhooks/github",
        content=raw_payload,
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Delivery": "deliv-1",
            "X-GitHub-Event": "push",
        },
    )
    assert bad_resp.status_code == 400
    assert "deliv-1" not in store.webhook_deliveries

    # 2. Верная подпись -> 204 No Content
    ok_resp = client.post(
        "/webhooks/github",
        content=raw_payload,
        headers={
            "X-Hub-Signature-256": valid_sig,
            "X-GitHub-Delivery": "deliv-1",
            "X-GitHub-Event": "push",
        },
    )
    assert ok_resp.status_code == 204

    # 3. Повторная доставка того же delivery_id -> 409 Conflict (дедупликация)
    dup_resp = client.post(
        "/webhooks/github",
        content=raw_payload,
        headers={
            "X-Hub-Signature-256": valid_sig,
            "X-GitHub-Delivery": "deliv-1",
            "X-GitHub-Event": "push",
        },
    )
    assert dup_resp.status_code == 409
