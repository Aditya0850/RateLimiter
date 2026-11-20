"""API routes for the Rate Limiter service."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import get_database
from app.exceptions import (
    DatabaseConnectionException,
    InvalidClientIdException,
    RateLimitExceededException,
)
from app.limiter import RateLimiter
from app.models import (
    ErrorResponse,
    HealthResponse,
    RateLimitInfo,
    RateLimitStatusResponse,
    RootResponse,
    TestEndpointResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def get_client_id(
    x_client_id: Annotated[str | None, Header(alias="X-Client-ID")] = None,
    request: Request = None,
) -> str:
    """Extract client identifier from request.

    Priority:
    1. X-Client-ID header (explicit client identification)
    2. Fallback to client IP (with trusted proxy support)

    Note: In production behind a load balancer/proxy, configure
    `trusted_proxy_count` in settings and use X-Forwarded-For header
    with proper validation. A client-controlled header can be spoofed;
    production systems should derive client identity from authenticated
    users, API keys, or a trusted proxy configuration.
    """
    if x_client_id is not None:
        # Header was provided - validate it
        stripped = x_client_id.strip()
        if not stripped:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid client identifier: client_id cannot be empty",
            )
        return stripped

    # Header not provided - fallback to client IP
    client_host = request.client.host if request.client else "unknown"
    return f"ip_{client_host}"


def get_rate_limiter() -> RateLimiter:
    """Dependency to get RateLimiter instance."""
    db = get_database()
    return RateLimiter(db)


def add_rate_limit_headers(response: Response, limit_info: RateLimitInfo) -> None:
    """Add standard rate limit headers to response."""
    response.headers["RateLimit-Limit"] = str(limit_info.limit)
    response.headers["RateLimit-Remaining"] = str(limit_info.remaining)
    response.headers["RateLimit-Reset"] = str(limit_info.reset)
    if limit_info.retry_after is not None:
        response.headers["Retry-After"] = str(limit_info.retry_after)


@router.get(
    "/",
    response_model=RootResponse,
    summary="Service information",
    description="Returns basic information about the rate limiter service.",
)
async def root() -> RootResponse:
    """Root endpoint - service information."""
    return RootResponse(
        service="Rate Limiter API",
        version="1.0.0",
        description="Fixed Window Counter rate limiting service",
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns service and database health status.",
)
async def health_check(limiter: RateLimiter = Depends(get_rate_limiter)) -> HealthResponse:
    """Health check endpoint - verifies database connectivity."""
    try:
        db_healthy = await limiter._collection.database.client.admin.command("ping")
        db_status = "connected" if db_healthy.get("ok") == 1 else "disconnected"
    except Exception as e:
        logger.warning(f"Health check failed: {e}")
        db_status = "disconnected"

    return HealthResponse(status="ok", database=db_status)


@router.get(
    "/api/test",
    response_model=TestEndpointResponse,
    summary="Test endpoint with rate limiting",
    description=(
        "Test endpoint that passes through the rate limiter. "
        "Returns 200 when allowed, 429 when rate limit exceeded."
    ),
    responses={
        200: {"model": TestEndpointResponse, "description": "Request allowed"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
)
async def test_endpoint(
    response: Response,
    client_id: str = Depends(get_client_id),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TestEndpointResponse:
    """Test endpoint that enforces rate limiting."""
    try:
        result = await limiter.check_rate_limit(client_id)

        limit_info = RateLimitInfo(
            limit=result.limit,
            remaining=result.remaining,
            reset=result.reset_timestamp,
            retry_after=result.retry_after,
        )
        add_rate_limit_headers(response, limit_info)

        if not result.allowed:
            # Return 429 with error response
            error_response = ErrorResponse(
                error="rate_limit_exceeded",
                message="Rate limit exceeded",
                detail={
                    "limit": result.limit,
                    "remaining": 0,
                    "reset_at": result.reset_timestamp,
                    "retry_after": result.retry_after,
                },
            )
            return JSONResponse(
                status_code=429,
                content=error_response.model_dump(),
                headers=dict(response.headers),
            )

        return TestEndpointResponse(
            message="Request allowed",
            rate_limit=limit_info,
        )

    except Exception as e:
        logger.error(f"Rate limit check failed for client {client_id}: {e}")
        raise DatabaseConnectionException("Failed to check rate limit")


@router.get(
    "/api/rate-limit/status",
    response_model=RateLimitStatusResponse,
    summary="Get rate limit status",
    description=(
        "Returns current rate limit status for the client WITHOUT consuming a request. "
        "Useful for checking remaining quota before making requests."
    ),
    responses={
        200: {"model": RateLimitStatusResponse, "description": "Current rate limit status"},
        400: {"model": ErrorResponse, "description": "Invalid client identifier"},
    },
)
async def rate_limit_status(
    client_id: str = Depends(get_client_id),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> RateLimitStatusResponse:
    """Get current rate limit status without consuming a request."""
    try:
        status = await limiter.get_status(client_id)

        return RateLimitStatusResponse(
            client_id=client_id,
            limit=status.limit,
            remaining=status.remaining,
            reset=status.reset_timestamp,
            request_count=status.request_count,
            window_start=status.window_start,
        )

    except ValueError as e:
        raise InvalidClientIdException(str(e))
    except Exception as e:
        logger.error(f"Status check failed for client {client_id}: {e}")
        raise DatabaseConnectionException("Failed to get rate limit status")