"""Pydantic models for the Rate Limiter API."""

from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


class RateLimitInfo(BaseModel):
    """Rate limit information returned in headers and responses."""

    limit: int = Field(..., description="Maximum requests allowed in window")
    remaining: int = Field(..., description="Requests remaining in current window")
    reset: int = Field(..., description="Unix timestamp when window resets")
    retry_after: Optional[int] = Field(
        None, description="Seconds until next request allowed (when limit exceeded)"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "limit": 5,
                "remaining": 3,
                "reset": 1732036860,
                "retry_after": None,
            }
        }
    )


class RateLimitStatusResponse(BaseModel):
    """Response for GET /api/rate-limit/status endpoint."""

    client_id: str = Field(..., description="Client identifier")
    limit: int = Field(..., description="Maximum requests allowed in window")
    remaining: int = Field(..., description="Requests remaining in current window")
    reset: int = Field(..., description="Unix timestamp when window resets")
    request_count: int = Field(..., description="Requests made in current window")
    window_start: int = Field(..., description="Unix timestamp of window start")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "client_id": "user_101",
                "limit": 5,
                "remaining": 2,
                "reset": 1732036860,
                "request_count": 3,
                "window_start": 1732036800,
            }
        }
    )


class TestEndpointResponse(BaseModel):
    """Response for GET /api/test endpoint."""

    message: str = Field(..., description="Success message")
    rate_limit: RateLimitInfo = Field(..., description="Rate limit information")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "Request allowed",
                "rate_limit": {
                    "limit": 5,
                    "remaining": 4,
                    "reset": 1732036860,
                    "retry_after": None,
                },
            }
        }
    )


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(..., description="Service status")
    database: str = Field(..., description="Database connection status")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"status": "ok", "database": "connected"}
        }
    )


class RootResponse(BaseModel):
    """Root endpoint response."""

    service: str = Field(..., description="Service name")
    version: str = Field(..., description="API version")
    description: str = Field(..., description="Service description")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "service": "Rate Limiter API",
                "version": "1.0.0",
                "description": "Fixed Window Counter rate limiting service",
            }
        }
    )


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str = Field(..., description="Error type")
    message: str = Field(..., description="Human-readable error message")
    detail: Optional[dict] = Field(None, description="Additional error details")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "rate_limit_exceeded",
                "message": "Rate limit exceeded",
                "detail": {
                    "limit": 5,
                    "remaining": 0,
                    "reset_at": 1732036860,
                    "retry_after": 45,
                },
            }
        }
    )