from __future__ import annotations

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "ai_merge_assistant",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Исключаем умножение retries между Celery и адаптером (ASSUMPTION T3)
    task_acks_late=True,
    task_default_max_retries=0,
)
