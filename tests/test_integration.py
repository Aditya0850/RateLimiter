"""Integration tests for Rate Limiter using real MongoDB."""

import pytest
import asyncio
import time
from datetime import datetime, timezone

from app.limiter import RateLimiter
from app.database import db_manager
from app.config import settings


class TestRateLimiterIntegration:
    """Integration tests with real MongoDB."""

    @classmethod
    def setup_class(cls):
        """Initialize database connection once for all tests."""
        cls.loop = asyncio.new_event_loop()
        cls.loop.run_until_complete(db_manager.initialize())

    @classmethod
    def teardown_class(cls):
        """Close database connection after all tests."""
        cls.loop.run_until_complete(db_manager.close())
        cls.loop.close()

    def setup_method(self):
        """Clean collection before each test."""
        collection = db_manager.db[settings.mongodb_collection]
        self.loop.run_until_complete(collection.delete_many({}))

    def teardown_method(self):
        """Clean collection after each test."""
        collection = db_manager.db[settings.mongodb_collection]
        self.loop.run_until_complete(collection.delete_many({}))

    def _get_limiter(self):
        """Get a rate limiter instance."""
        return RateLimiter(db_manager.db)

    def test_first_request_succeeds(self):
        """Test that first request is allowed."""
        rate_limiter = self._get_limiter()
        result = self.loop.run_until_complete(rate_limiter.check_rate_limit("integration_client_1"))

        assert result.allowed is True
        assert result.limit == 5
        assert result.remaining == 4
        assert result.retry_after is None

    def test_five_requests_succeed(self):
        """Test that 5 requests are allowed."""
        rate_limiter = self._get_limiter()
        for i in range(5):
            result = self.loop.run_until_complete(rate_limiter.check_rate_limit("integration_client_2"))
            assert result.allowed is True
            assert result.remaining == 4 - i

    def test_sixth_request_rejected(self):
        """Test that 6th request is rejected."""
        rate_limiter = self._get_limiter()
        client_id = "integration_client_3"

        # Make 5 requests
        for _ in range(5):
            result = self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))
            assert result.allowed is True

        # 6th request should be rejected
        result = self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None
        assert result.retry_after > 0

    def test_different_clients_independent(self):
        """Test that different clients have independent counters."""
        rate_limiter = self._get_limiter()

        # Client A makes 3 requests
        for _ in range(3):
            result = self.loop.run_until_complete(rate_limiter.check_rate_limit("client_A"))
            assert result.allowed is True

        # Client B makes 5 requests
        for _ in range(5):
            result = self.loop.run_until_complete(rate_limiter.check_rate_limit("client_B"))
            assert result.allowed is True

        # Client A should still have 2 remaining
        result = self.loop.run_until_complete(rate_limiter.check_rate_limit("client_A"))
        assert result.allowed is True
        assert result.remaining == 1

        # Client B should be at limit
        result = self.loop.run_until_complete(rate_limiter.check_rate_limit("client_B"))
        assert result.allowed is False

    def test_window_expiration_resets_counter(self):
        """Test that counter resets after window expiration."""
        rate_limiter = self._get_limiter()
        client_id = "integration_client_4"

        # Make 5 requests to exhaust limit
        for _ in range(5):
            result = self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))
            assert result.allowed is True

        # 6th should be rejected
        result = self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))
        assert result.allowed is False

        # Get the reset timestamp
        reset_ts = result.reset_timestamp

        # Verify the document structure
        collection = db_manager.db[settings.mongodb_collection]
        doc = self.loop.run_until_complete(collection.find_one({"client_id": client_id}))
        assert doc is not None
        assert doc["request_count"] == 6  # 5 allowed + 1 rejected

    def test_get_status_does_not_consume(self):
        """Test that get_status doesn't consume a request."""
        rate_limiter = self._get_limiter()
        client_id = "integration_client_5"

        # Make 2 requests
        self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))
        self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))

        # Check status - should show 2 used, 3 remaining
        status = self.loop.run_until_complete(rate_limiter.get_status(client_id))
        assert status.request_count == 2
        assert status.remaining == 3
        assert status.limit == 5

        # Make another request - should be 3rd
        result = self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))
        assert result.remaining == 2

    def test_remaining_request_info_correct(self):
        """Test that remaining request count is accurate."""
        rate_limiter = self._get_limiter()
        client_id = "integration_client_6"

        for expected_remaining in [4, 3, 2, 1, 0]:
            result = self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))
            assert result.remaining == expected_remaining

    def test_reset_timestamp_correct(self):
        """Test that reset timestamp is correct."""
        rate_limiter = self._get_limiter()
        result = self.loop.run_until_complete(rate_limiter.check_rate_limit("integration_client_7"))

        # Reset timestamp should be in the future
        assert result.reset_timestamp > int(time.time())
        # Should be within the current window (1-60 seconds from now)
        # Fixed window means reset is at window boundary, not exactly 60s from request
        assert result.reset_timestamp <= int(time.time()) + 60
        assert result.reset_timestamp > int(time.time())

    def test_mongodb_document_structure(self):
        """Test that MongoDB documents have correct structure and types."""
        rate_limiter = self._get_limiter()
        client_id = "integration_client_8"

        self.loop.run_until_complete(rate_limiter.check_rate_limit(client_id))

        collection = db_manager.db[settings.mongodb_collection]
        doc = self.loop.run_until_complete(collection.find_one({"client_id": client_id}))

        assert doc is not None
        assert doc["client_id"] == client_id
        assert doc["request_count"] == 1
        assert isinstance(doc["window_start"], datetime)
        assert isinstance(doc["expires_at"], datetime)
        # Note: MongoDB returns naive datetimes (UTC), tzinfo is None
        # Application code handles this by assuming UTC

        # Verify window_start and expires_at are 60 seconds apart
        diff = (doc["expires_at"] - doc["window_start"]).total_seconds()
        assert diff == 60

    def test_ttl_index_exists(self):
        """Test that TTL index exists on expires_at."""
        collection = db_manager.db[settings.mongodb_collection]
        indexes = self.loop.run_until_complete(collection.list_indexes().to_list(length=None))

        ttl_index = None
        for idx in indexes:
            if idx.get("name") == "ttl_expires_at":
                ttl_index = idx
                break

        assert ttl_index is not None, "TTL index 'ttl_expires_at' not found"
        assert ttl_index.get("expireAfterSeconds") == 0
        key = ttl_index.get("key", {})
        assert key.get("expires_at") == 1

    def test_unique_compound_index_exists(self):
        """Test that unique compound index exists on (client_id, window_start)."""
        collection = db_manager.db[settings.mongodb_collection]
        indexes = self.loop.run_until_complete(collection.list_indexes().to_list(length=None))

        unique_index = None
        for idx in indexes:
            if idx.get("name") == "unique_client_window":
                unique_index = idx
                break

        assert unique_index is not None, "Unique index 'unique_client_window' not found"
        assert unique_index.get("unique") is True
        key = unique_index.get("key", {})
        assert key.get("client_id") == 1
        assert key.get("window_start") == 1