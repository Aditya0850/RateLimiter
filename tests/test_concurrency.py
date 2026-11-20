"""Concurrency tests for Rate Limiter."""

import pytest
import asyncio
import time
from datetime import datetime, timezone

from app.limiter import RateLimiter
from app.database import db_manager
from app.config import settings


class TestRateLimiterConcurrency:
    """Concurrency tests with real MongoDB."""

    @classmethod
    def setup_class(cls):
        """Initialize database connection once for all tests."""
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
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

    def _run_async(self, coro):
        """Run a coroutine in the test event loop."""
        return self.loop.run_until_complete(coro)

    def test_concurrent_requests_do_not_exceed_limit(self):
        """Test that concurrent requests cannot exceed the configured limit.

        This test simulates multiple simultaneous requests from the same client.
        The atomic find_one_and_update with upsert ensures that the counter
        never exceeds the limit due to race conditions.
        """
        rate_limiter = self._get_limiter()
        client_id = "concurrent_client_1"
        num_concurrent = 20  # More than the limit of 5
        limit = 5

        async def make_request():
            return await rate_limiter.check_rate_limit(client_id)

        # Launch all requests concurrently
        tasks = [make_request() for _ in range(num_concurrent)]
        results = self._run_async(asyncio.gather(*tasks))

        # Count allowed requests
        allowed_count = sum(1 for r in results if r.allowed)
        rejected_count = sum(1 for r in results if not r.allowed)

        # Exactly 5 should be allowed, rest rejected
        assert allowed_count == limit, f"Expected {limit} allowed, got {allowed_count}"
        assert rejected_count == num_concurrent - limit

        # Verify final state in database - all requests are counted, including rejected ones
        collection = db_manager.db[settings.mongodb_collection]
        doc = self._run_async(collection.find_one({"client_id": client_id}))
        assert doc["request_count"] == num_concurrent  # All requests increment counter

    def test_concurrent_different_clients_independent(self):
        """Test that concurrent requests from different clients are independent."""
        rate_limiter = self._get_limiter()
        num_clients = 10
        requests_per_client = 3
        limit = 5

        async def make_client_requests(client_id):
            results = []
            for _ in range(requests_per_client):
                result = await rate_limiter.check_rate_limit(client_id)
                results.append(result)
            return results

        # Launch concurrent requests from multiple clients
        client_ids = [f"concurrent_client_{i}" for i in range(num_clients)]
        tasks = [make_client_requests(cid) for cid in client_ids]
        all_results = self._run_async(asyncio.gather(*tasks))

        # Each client should have all requests allowed (3 < 5)
        for client_results in all_results:
            for result in client_results:
                assert result.allowed is True

        # Verify each client has exactly 3 requests in database
        collection = db_manager.db[settings.mongodb_collection]
        for client_id in client_ids:
            doc = self._run_async(collection.find_one({"client_id": client_id}))
            assert doc["request_count"] == requests_per_client

    def test_window_boundary_transition(self):
        """Test behavior at window boundary.

        This test verifies that requests near the window boundary
        are correctly assigned to the appropriate window.
        """
        rate_limiter = self._get_limiter()
        client_id = "boundary_client"

        # Get current window boundaries using the limiter's own calculation
        window_start, window_end = rate_limiter._calculate_window()

        # Make requests at the end of current window
        for _ in range(3):
            result = self._run_async(rate_limiter.check_rate_limit(client_id))
            assert result.allowed is True

        # Verify they're in the current window
        collection = db_manager.db[settings.mongodb_collection]
        doc = self._run_async(collection.find_one({"client_id": client_id}))
        assert doc["request_count"] == 3
        # MongoDB returns naive datetime (UTC), so we need to handle it
        window_start_dt = doc["window_start"]
        if window_start_dt.tzinfo is None:
            window_start_dt = window_start_dt.replace(tzinfo=timezone.utc)
        assert window_start_dt.timestamp() == window_start

        # Note: We can't easily test the next window without time manipulation
        # In production, the deterministic window calculation ensures correct assignment

    def test_duplicate_key_race_condition(self):
        """Test handling of duplicate-key upsert race condition.

        When multiple requests arrive simultaneously for a new client/window,
        they may all try to upsert. The unique index prevents duplicates,
        and the code retries on DuplicateKeyError.
        """
        rate_limiter = self._get_limiter()
        client_id = "race_client"
        num_concurrent = 10

        async def make_request():
            return await rate_limiter.check_rate_limit(client_id)

        # All requests arrive at exactly the same time for a fresh client/window
        tasks = [make_request() for _ in range(num_concurrent)]
        results = self._run_async(asyncio.gather(*tasks))

        # Exactly 5 should succeed
        allowed = sum(1 for r in results if r.allowed)
        rejected = sum(1 for r in results if not r.allowed)

        assert allowed == 5
        assert rejected == 5

        # Verify database state - all 10 concurrent requests increment the counter
        collection = db_manager.db[settings.mongodb_collection]
        doc = self._run_async(collection.find_one({"client_id": client_id}))
        assert doc["request_count"] == num_concurrent

    def test_high_concurrency_stress(self):
        """Stress test with high concurrency."""
        rate_limiter = self._get_limiter()
        client_id = "stress_client"
        num_requests = 100
        limit = 5

        async def make_request():
            return await rate_limiter.check_rate_limit(client_id)

        tasks = [make_request() for _ in range(num_requests)]
        results = self._run_async(asyncio.gather(*tasks))

        allowed = sum(1 for r in results if r.allowed)
        rejected = sum(1 for r in results if not r.allowed)

        assert allowed == limit
        assert rejected == num_requests - limit

        # Database should show all requests (including rejected ones)
        collection = db_manager.db[settings.mongodb_collection]
        doc = self._run_async(collection.find_one({"client_id": client_id}))
        assert doc["request_count"] == num_requests

    def test_concurrent_status_checks(self):
        """Test concurrent status checks while requests are being made."""
        rate_limiter = self._get_limiter()
        client_id = "status_client"

        # Make some requests first
        for _ in range(3):
            self._run_async(rate_limiter.check_rate_limit(client_id))

        async def check_status():
            return await rate_limiter.get_status(client_id)

        # Concurrent status checks should not affect request count
        tasks = [check_status() for _ in range(20)]
        statuses = self._run_async(asyncio.gather(*tasks))

        for status in statuses:
            assert status.request_count == 3
            assert status.remaining == 2
            assert status.limit == 5

        # Verify request count unchanged
        collection = db_manager.db[settings.mongodb_collection]
        doc = self._run_async(collection.find_one({"client_id": client_id}))
        assert doc["request_count"] == 3