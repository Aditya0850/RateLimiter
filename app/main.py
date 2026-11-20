"""FastAPI application entry point for Rate Limiter API."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import lifespan, db_manager
from app.exceptions import RateLimiterException
from app.models import ErrorResponse
from app.routes import router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Rate Limiter API...")
    async with lifespan(app):
        logger.info("Rate Limiter API started successfully")
        yield

    # Shutdown
    logger.info("Shutting down Rate Limiter API...")
    logger.info("Rate Limiter API stopped")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title="Rate Limiter API",
        description="Fixed Window Counter rate limiting service",
        version="1.0.0",
        lifespan=app_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS middleware - configure for production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure for production
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # Global exception handler for custom exceptions
    @app.exception_handler(RateLimiterException)
    async def rate_limiter_exception_handler(request, exc: RateLimiterException):
        error_response = ErrorResponse(
            error=exc.__class__.__name__.replace("Exception", "").lower(),
            message=exc.message,
            detail=exc.detail if exc.detail else None,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response.model_dump(),
            headers=exc.headers,
        )

    # Include routes
    app.include_router(router)

    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )