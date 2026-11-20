"""Custom exceptions for the Rate Limiter API."""

from typing import Optional, Dict, Any


class RateLimiterException(Exception):
    """Base exception for rate limiter errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        headers: Optional[Dict[str, str]] = None,
        detail: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}
        self.detail = detail or {}
        super().__init__(message)


class RateLimitExceededException(RateLimiterException):
    """Raised when rate limit is exceeded."""

    def __init__(
        self,
        limit: int,
        remaining: int,
        reset_timestamp: int,
        retry_after: int,
    ):
        headers = {
            "RateLimit-Limit": str(limit),
            "RateLimit-Remaining": "0",
            "RateLimit-Reset": str(reset_timestamp),
            "Retry-After": str(retry_after),
        }
        super().__init__(
            message="Rate limit exceeded",
            status_code=429,
            headers=headers,
            detail={
                "limit": limit,
                "remaining": 0,
                "reset_at": reset_timestamp,
                "retry_after": retry_after,
            },
        )


class InvalidClientIdException(RateLimiterException):
    """Raised when client_id is invalid."""

    def __init__(self, message: str = "Invalid client identifier"):
        super().__init__(
            message=message,
            status_code=400,
        )


class DatabaseConnectionException(RateLimiterException):
    """Raised when database connection fails."""

    def __init__(self, message: str = "Database connection error"):
        super().__init__(
            message=message,
            status_code=503,
        )