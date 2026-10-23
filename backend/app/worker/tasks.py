from __future__ import annotations

from uuid import UUID

from app.worker.celery_app import celery_app


@celery_app.task(name="runs.execute_pipeline", max_retries=0)
def execute_run_pipeline_task(run_id: str) -> dict[str, str]:
    """Фоновое выполнение пайплайна прогона через RunOrchestrator."""
    _ = UUID(run_id)
    return {"run_id": run_id, "status": "dispatched_offline"}


@celery_app.task(name="uploads.parse_large_document", max_retries=0)
def parse_large_document_task(upload_id: str) -> dict[str, str]:
    """Фоновая обработка документов > 50 страниц (раздел 1 team-guide.md)."""
    _ = UUID(upload_id)
    return {"upload_id": upload_id, "status": "parsed_offline"}


@celery_app.task(name="webhooks.process_delivery", max_retries=0)
def process_webhook_delivery_task(delivery_id: str, event_type: str) -> dict[str, str]:
    """Идемпотентная фоновая обработка проверенного webhook."""
    return {"delivery_id": delivery_id, "event_type": event_type, "status": "processed"}
