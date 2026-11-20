"""Unit tests for Rate Limiter engine (mocked database)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
import time

from app.limiter import RateLimiter, RateLimitResult, RateLimitStatus
from app.config import Settings
from pymongo.errors import DuplicateKeyError


# Fixed timestamp for consistent testing
TEST_TIMESTAMP = 1795178400  # 2026-11-19 12:00:00 UTC
TEST_WINDOW_START = 1795178400
TEST_WINDOW_END = 1795178460


def create_mock_collection():
    """Create a mock collection with async methods."""
    collection = MagicMock()

    # Storage for return values
    collection._find_one_and_update_return_value = None
    collection._find_one_return_value = None
    collection._find_one_and_update_side_effect = None

    async def mock_find_one_and_update(*args, **kwargs):
        if collection._find_one_and_update_side_effect:
            return await collection._find_one_and_update_side_effect(*args, **kwargs)
        return collection._find_one_and_update_return_value

    async def mock_find_one(*args, **kwargs):
        return collection._find_one_return_value

    async def mock_delete_one(*args, **kwargs):
        return MagicMock(deleted_count=1)

    collection.find_one_and_update = mock_find_one_and_update
    collection.find_one = mock_find_one
    collection.delete_one = mock_delete_one

    return collection


def create_mock_database(mock_collection):
    """Create a mock database."""
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=mock_collection)
    return db


def create_rate_limiter(mock_database):
    """Create RateLimiter with mocked database and settings."""
    mock_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database="rate_limiter_db",
        mongodb_collection="rate_limits",
        rate_limit_max_requests=5,
        rate_limit_window_seconds=60,
    )
    with patch("app.limiter.settings", mock_settings):
        limiter = RateLimiter(mock_database)
        return limiter


class TestRateLimiterUnit:
    """Unit tests with mocked database."""

    @pytest.mark.asyncio
    async def test_first_request_succeeds(self):
        """Test that first request is allowed."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        mock_collection._find_one_and_update_return_value = {
            "client_id": "test_client",
            "request_count": 1,
            "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
            "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
        }

        with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
            result = await rate_limiter.check_rate_limit("test_client")

        assert result.allowed is True
        assert result.limit == 5
        assert result.remaining == 4
        assert result.reset_timestamp == TEST_WINDOW_END
        assert result.retry_after is None

    @pytest.mark.asyncio
    async def test_five_requests_succeed(self):
        """Test that 5 requests are allowed."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        for i in range(1, 6):
            mock_collection._find_one_and_update_return_value = {
                "client_id": "test_client",
                "request_count": i,
                "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
                "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
            }

            with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
                result = await rate_limiter.check_rate_limit("test_client")
            assert result.allowed is True
            assert result.remaining == 5 - i

    @pytest.mark.asyncio
    async def test_sixth_request_rejected(self):
        """Test that 6th request is rejected with 429."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        # First 5 requests succeed
        for i in range(1, 6):
            mock_collection._find_one_and_update_return_value = {
                "client_id": "test_client",
                "request_count": i,
                "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
                "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
            }
            with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
                await rate_limiter.check_rate_limit("test_client")

        # 6th request should be rejected
        mock_collection._find_one_and_update_return_value = {
            "client_id": "test_client",
            "request_count": 6,
            "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
            "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
        }

        with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
            result = await rate_limiter.check_rate_limit("test_client")

        assert result.allowed is False
        assert result.limit == 5
        assert result.remaining == 0
        assert result.retry_after is not None
        assert result.retry_after > 0

    @pytest.mark.asyncio
    async def test_different_clients_independent(self):
        """Test that different clients have independent counters."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        call_count = {"client_a": 0, "client_b": 0}

        async def mock_find_one_and_update(filter=None, update=None, upsert=None, return_document=None, **kwargs):
            client_id = filter["client_id"]
            call_count[client_id] += 1
            return {
                "client_id": client_id,
                "request_count": call_count[client_id],
                "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
                "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
            }

        mock_collection.find_one_and_update = mock_find_one_and_update

        with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
            # Client A makes 3 requests
            for _ in range(3):
                result = await rate_limiter.check_rate_limit("client_a")
                assert result.allowed is True

            # Client B makes 2 requests
            for _ in range(2):
                result = await rate_limiter.check_rate_limit("client_b")
                assert result.allowed is True

        # Verify independent counts
        assert call_count["client_a"] == 3
        assert call_count["client_b"] == 2

    @pytest.mark.asyncio
    async def test_window_expiration(self):
        """Test that counter resets after window expiration."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        # First request in window
        mock_collection._find_one_and_update_return_value = {
            "client_id": "test_client",
            "request_count": 1,
            "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
            "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
        }

        with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
            result = await rate_limiter.check_rate_limit("test_client")
        assert result.allowed is True
        assert result.remaining == 4

        # Simulate window expired
        expired_doc = {
            "client_id": "test_client",
            "request_count": 5,
            "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
            "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
        }
        fresh_doc = {
            "client_id": "test_client",
            "request_count": 1,
            "window_start": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
            "expires_at": datetime.fromtimestamp(TEST_WINDOW_END + 60, tz=timezone.utc),
        }

        call_count = [0]
        async def mock_find_one_and_update_expired(filter=None, update=None, upsert=None, return_document=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return expired_doc
            return fresh_doc

        mock_collection.find_one_and_update = mock_find_one_and_update_expired
        mock_collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))

        # Mock time to be after expiry (next window)
        next_window_time = TEST_WINDOW_END + 1
        with patch("app.limiter.time.time", return_value=next_window_time):
            result = await rate_limiter.check_rate_limit("test_client")

        assert result.allowed is True
        assert result.remaining == 4  # Fresh window

    @pytest.mark.asyncio
    async def test_empty_client_id_raises(self):
        """Test that empty client_id raises ValueError."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        with pytest.raises(ValueError, match="client_id cannot be empty"):
            await rate_limiter.check_rate_limit("")

        with pytest.raises(ValueError, match="client_id cannot be empty"):
            await rate_limiter.check_rate_limit("   ")

    @pytest.mark.asyncio
    async def test_get_status_does_not_consume(self):
        """Test that get_status doesn't consume a request."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        mock_collection._find_one_return_value = {
            "client_id": "test_client",
            "request_count": 3,
            "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
            "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
        }

        with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
            status = await rate_limiter.get_status("test_client")

        assert status.limit == 5
        assert status.remaining == 2
        assert status.request_count == 3
        assert status.window_start == TEST_WINDOW_START
        assert status.reset_timestamp == TEST_WINDOW_END

    @pytest.mark.asyncio
    async def test_get_status_no_existing_window(self):
        """Test get_status when client has no requests in current window."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        mock_collection._find_one_return_value = None

        with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
            status = await rate_limiter.get_status("new_client")

        assert status.limit == 5
        assert status.remaining == 5
        assert status.request_count == 0

    @pytest.mark.asyncio
    async def test_duplicate_key_retry(self):
        """Test handling of DuplicateKeyError with retry."""
        mock_collection = create_mock_collection()
        mock_database = create_mock_database(mock_collection)
        rate_limiter = create_rate_limiter(mock_database)

        call_count = [0]
        async def mock_find_one_and_update_duplicate(filter=None, update=None, upsert=None, return_document=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise DuplicateKeyError("duplicate key error")
            return {
                "client_id": "test_client",
                "request_count": 2,
                "window_start": datetime.fromtimestamp(TEST_WINDOW_START, tz=timezone.utc),
                "expires_at": datetime.fromtimestamp(TEST_WINDOW_END, tz=timezone.utc),
            }

        mock_collection.find_one_and_update = mock_find_one_and_update_duplicate

        with patch("app.limiter.time.time", return_value=TEST_TIMESTAMP):
            result = await rate_limiter.check_rate_limit("test_client")

        assert result.allowed is True
        assert result.remaining == 3
        assert call_count[0] == 2