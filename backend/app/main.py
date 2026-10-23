from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import APIErrorException, api_router
from app.core.config import settings


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0-T1",
        description="AI-Merge-Assistant Backend API (Schema Version T1)",
    )

    @app.exception_handler(APIErrorException)
    async def api_error_handler(_: Request, exc: APIErrorException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload)

    @app.get("/health", tags=["health"])
    def health_check() -> dict[str, str]:
        return {"status": "ok", "schema_version": "T1"}

    app.include_router(api_router)
    return app


app = create_app()
