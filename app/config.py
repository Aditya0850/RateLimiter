"""Configuration settings for the Rate Limiter application."""

from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import ConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # MongoDB
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "rate_limiter_db"
    mongodb_collection: str = "rate_limits"

    # Rate Limiter
    rate_limit_max_requests: int = 5
    rate_limit_window_seconds: int = 60

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False

    # Security
    trusted_proxy_count: int = 0  # Number of trusted proxies (0 = no proxy support)

    # Timeouts
    request_timeout_seconds: float = 30.0
    mongodb_timeout_ms: int = 5000

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Export settings instance for easy import
settings = get_settings()