"""Rate Limiter Engine - Fixed Window Counter Implementation.

This module implements a thread-safe, atomic Fixed Window Counter rate limiter
using MongoDB as the backend storage.

Algorithm: Fixed Window Counter
- Time is divided into fixed windows of size `window_seconds`
- Each window is identified by its start timestamp: floor(now / window_seconds) * window_seconds
- Each client has an independent counter per window
- Counters are stored in MongoDB with atomic operations

Key Design Decisions:
1. Atomic Operations: Uses find_one_and_update with upsert=True for atomic increment
2. Expiry Validation: Application-level validation of expires_at (never rely on TTL alone)
3. Duplicate-Key Handling: Catches DuplicateKeyError and retries once
4. Window Boundaries: Deterministic Unix time buckets ensure consistent window assignment
5. UTC Timestamps: All timestamps stored as UTC datetime objects
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    """Result of a rate limit check."""
    allowed: bool
    limit: int
    remaining: int
    reset_timestamp: int  # Unix timestamp when the window resets
    retry_after: Optional[int] = None  # Seconds until next window (when rejected)


@dataclass(frozen=True)
class RateLimitStatus:
    """Current rate limit status for a client (does not consume a request)."""
    limit: int
    remaining: int
    reset_timestamp: int
    request_count: int
    window_start: int


class RateLimiter:
    """Fixed Window Counter rate limiter with atomic MongoDB operations."""

    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._db = database
        self._collection = database[settings.mongodb_collection]
        self._max_requests = settings.rate_limit_max_requests
        self._window_seconds = settings.rate_limit_window_seconds

    def _calculate_window(self, timestamp: Optional[float] = None) -> tuple[int, int]:
        """Calculate window boundaries for a given timestamp.

        Args:
            timestamp: Unix timestamp (default: current time)

        Returns:
            Tuple of (window_start, expires_at) as Unix timestamps
        """
        now = timestamp or time.time()
        window_start = int(now // self._window_seconds) * self._window_seconds
        expires_at = window_start + self._window_seconds
        return window_start, expires_at

    def _to_datetime(self, unix_timestamp: int) -> datetime:
        """Convert Unix timestamp to UTC datetime for MongoDB storage."""
        return datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)

    async def check_rate_limit(self, client_id: str) -> RateLimitResult:
        """Check and consume a rate limit slot for the client.

        Uses atomic find_one_and_update with upsert to ensure:
        - Only one document per client per window (enforced by unique index)
        - Counter increments atomically
        - Race conditions are handled via duplicate-key retry

        Args:
            client_id: Unique identifier for the client

        Returns:
            RateLimitResult with allowance decision and headers
        """
        if not client_id or not client_id.strip():
            raise ValueError("client_id cannot be empty")

        client_id = client_id.strip()
        window_start, expires_at = self._calculate_window()

        # Convert to UTC datetime for MongoDB
        window_start_dt = self._to_datetime(window_start)
        expires_at_dt = self._to_datetime(expires_at)

        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                # Atomic upsert: increment counter or create new document
                # Using find_one_and_update with upsert=True ensures atomicity
                result = await self._collection.find_one_and_update(
                    filter={
                        "client_id": client_id,
                        "window_start": window_start_dt,
                    },
                    update={
                        "$inc": {"request_count": 1},
                        "$setOnInsert": {
                            "client_id": client_id,
                            "window_start": window_start_dt,
                            "expires_at": expires_at_dt,
                        },
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )

                if result is None:
                    # Should not happen with upsert=True, but handle gracefully
                    logger.warning(f"Upsert returned None for client {client_id}, retrying")
                    continue

                request_count = result["request_count"]

                # Validate expiry at application level (TTL is async, don't rely on it)
                stored_expires_at = result["expires_at"]
                if isinstance(stored_expires_at, datetime):
                    # Handle naive datetime from MongoDB - assume UTC
                    if stored_expires_at.tzinfo is None:
                        stored_expires_ts = int(stored_expires_at.replace(tzinfo=timezone.utc).timestamp())
                    else:
                        stored_expires_ts = int(stored_expires_at.timestamp())
                else:
                    stored_expires_ts = int(stored_expires_at)

                current_time = time.time()
                if current_time >= stored_expires_ts:
                    # Window has expired - this document is stale
                    # Delete it and retry to get a fresh window
                    logger.debug(f"Stale document for client {client_id}, deleting and retrying")
                    await self._collection.delete_one({
                        "client_id": client_id,
                        "window_start": window_start_dt,
                    })
                    continue

                # Calculate remaining requests
                remaining = max(0, self._max_requests - request_count)
                allowed = request_count <= self._max_requests

                if not allowed:
                    # Request exceeds limit - decrement counter since we shouldn't have consumed
                    # Actually, with Fixed Window, we count the request that exceeds
                    # The client gets 429 and must wait for window reset
                    retry_after = max(1, stored_expires_ts - int(current_time))
                    return RateLimitResult(
                        allowed=False,
                        limit=self._max_requests,
                        remaining=0,
                        reset_timestamp=stored_expires_ts,
                        retry_after=retry_after,
                    )

                return RateLimitResult(
                    allowed=True,
                    limit=self._max_requests,
                    remaining=remaining,
                    reset_timestamp=stored_expires_ts,
                )

            except DuplicateKeyError:
                # Another request created the document concurrently
                # Retry once to get the updated count
                if attempt < max_retries:
                    logger.debug(f"DuplicateKeyError for client {client_id}, retrying (attempt {attempt + 1})")
                    continue
                logger.warning(f"DuplicateKeyError after retries for client {client_id}")
                # Fall through to error handling

            except OperationFailure as e:
                logger.error(f"Database operation failed for client {client_id}: {e}")
                raise

        # If we exhausted retries, do a fresh read to get current state
        doc = await self._collection.find_one({
            "client_id": client_id,
            "window_start": window_start_dt,
        })

        if doc:
            request_count = doc["request_count"]
            stored_expires_at = doc["expires_at"]
            if isinstance(stored_expires_at, datetime):
                stored_expires_ts = int(stored_expires_at.timestamp())
            else:
                stored_expires_ts = int(stored_expires_at)

            remaining = max(0, self._max_requests - request_count)
            allowed = request_count <= self._max_requests

            if not allowed:
                retry_after = max(1, stored_expires_ts - int(time.time()))
                return RateLimitResult(
                    allowed=False,
                    limit=self._max_requests,
                    remaining=0,
                    reset_timestamp=stored_expires_ts,
                    retry_after=retry_after,
                )

            return RateLimitResult(
                allowed=True,
                limit=self._max_requests,
                remaining=remaining,
                reset_timestamp=stored_expires_ts,
            )

        # Fallback: window likely expired between retries, allow request
        return RateLimitResult(
            allowed=True,
            limit=self._max_requests,
            remaining=self._max_requests - 1,
            reset_timestamp=expires_at,
        )

    async def get_status(self, client_id: str) -> RateLimitStatus:
        """Get current rate limit status without consuming a request.

        Args:
            client_id: Unique identifier for the client

        Returns:
            RateLimitStatus with current counters
        """
        if not client_id or not client_id.strip():
            raise ValueError("client_id cannot be empty")

        client_id = client_id.strip()
        window_start, expires_at = self._calculate_window()
        window_start_dt = self._to_datetime(window_start)

        doc = await self._collection.find_one({
            "client_id": client_id,
            "window_start": window_start_dt,
        })

        if doc is None:
            # No requests in current window
            return RateLimitStatus(
                limit=self._max_requests,
                remaining=self._max_requests,
                reset_timestamp=expires_at,
                request_count=0,
                window_start=window_start,
            )

        request_count = doc["request_count"]
        stored_expires_at = doc["expires_at"]
        if isinstance(stored_expires_at, datetime):
            # Handle naive datetime from MongoDB - assume UTC
            if stored_expires_at.tzinfo is None:
                stored_expires_ts = int(stored_expires_at.replace(tzinfo=timezone.utc).timestamp())
            else:
                stored_expires_ts = int(stored_expires_at.timestamp())
        else:
            stored_expires_ts = int(stored_expires_at)

        # Validate expiry
        current_time = time.time()
        if current_time >= stored_expires_ts:
            # Window expired, return fresh window status
            return RateLimitStatus(
                limit=self._max_requests,
                remaining=self._max_requests,
                reset_timestamp=expires_at,
                request_count=0,
                window_start=window_start,
            )

        remaining = max(0, self._max_requests - request_count)
        return RateLimitStatus(
            limit=self._max_requests,
            remaining=remaining,
            reset_timestamp=stored_expires_ts,
            request_count=request_count,
            window_start=window_start,
        )

    async def reset_client(self, client_id: str) -> bool:
        """Reset rate limit for a specific client (admin operation).

        Args:
            client_id: Client identifier to reset

        Returns:
            True if any documents were deleted
        """
        if not client_id or not client_id.strip():
            raise ValueError("client_id cannot be empty")

        client_id = client_id.strip()
        result = await self._collection.delete_many({"client_id": client_id})
        return result.deleted_count > 0