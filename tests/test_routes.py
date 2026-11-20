"""Integration tests for FastAPI endpoints using real MongoDB."""

import pytest
import asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import db_manager
from app.config import settings


class TestRoutesIntegration:
    """Integration tests for API endpoints with real MongoDB."""

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

    async def _make_request(self, method: str, path: str, headers: dict = None):
        """Make an async request to the app."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.request(method, path, headers=headers)
            return response

    def _run_async(self, coro):
        """Run a coroutine in the test event loop."""
        return self.loop.run_until_complete(coro)

    def test_root_endpoint(self):
        """Test root endpoint returns service information."""
        response = self._run_async(self._make_request("GET", "/"))

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "Rate Limiter API"
        assert data["version"] == "1.0.0"
        assert "Fixed Window Counter" in data["description"]

    def test_health_endpoint(self):
        """Test health endpoint returns database status."""
        response = self._run_async(self._make_request("GET", "/health"))

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["database"] == "connected"

    def test_test_endpoint_first_request(self):
        """Test first request to /api/test is allowed."""
        response = self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": "test_user_1"}
        ))

        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Request allowed"
        assert data["rate_limit"]["limit"] == 5
        assert data["rate_limit"]["remaining"] == 4
        assert "reset" in data["rate_limit"]
        assert data["rate_limit"]["retry_after"] is None

        # Check headers
        assert response.headers["RateLimit-Limit"] == "5"
        assert response.headers["RateLimit-Remaining"] == "4"
        assert "RateLimit-Reset" in response.headers

    def test_test_endpoint_five_requests_allowed(self):
        """Test five requests are allowed."""
        client_id = "test_user_2"

        for i in range(5):
            response = self._run_async(self._make_request(
                "GET", "/api/test", headers={"X-Client-ID": client_id}
            ))
            assert response.status_code == 200
            data = response.json()
            assert data["message"] == "Request allowed"
            assert data["rate_limit"]["remaining"] == 4 - i

    def test_test_endpoint_sixth_request_rejected(self):
        """Test sixth request returns 429."""
        client_id = "test_user_3"

        # Make 5 requests
        for _ in range(5):
            response = self._run_async(self._make_request(
                "GET", "/api/test", headers={"X-Client-ID": client_id}
            ))
            assert response.status_code == 200

        # 6th request should be rejected
        response = self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": client_id}
        ))
        assert response.status_code == 429

        data = response.json()
        assert data["error"] == "rate_limit_exceeded"
        assert data["message"] == "Rate limit exceeded"
        assert data["detail"]["limit"] == 5
        assert data["detail"]["remaining"] == 0
        assert data["detail"]["retry_after"] is not None
        assert data["detail"]["retry_after"] > 0

        # Check headers on 429
        assert response.headers["RateLimit-Limit"] == "5"
        assert response.headers["RateLimit-Remaining"] == "0"
        assert "Retry-After" in response.headers

    def test_different_clients_independent(self):
        """Test different clients have independent rate limits."""

        # Client A makes 3 requests
        for _ in range(3):
            response = self._run_async(self._make_request(
                "GET", "/api/test", headers={"X-Client-ID": "client_A"}
            ))
            assert response.status_code == 200

        # Client B makes 5 requests
        for _ in range(5):
            response = self._run_async(self._make_request(
                "GET", "/api/test", headers={"X-Client-ID": "client_B"}
            ))
            assert response.status_code == 200

        # Client A should still have 2 remaining
        response = self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": "client_A"}
        ))
        assert response.status_code == 200
        assert response.json()["rate_limit"]["remaining"] == 1

        # Client B should be at limit
        response = self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": "client_B"}
        ))
        assert response.status_code == 429

    def test_status_endpoint_does_not_consume(self):
        """Test /api/rate-limit/status doesn't consume a request."""
        client_id = "test_user_4"

        # Make 2 requests
        self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": client_id}
        ))
        self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": client_id}
        ))

        # Check status - should show 2 used, 3 remaining
        response = self._run_async(self._make_request(
            "GET", "/api/rate-limit/status", headers={"X-Client-ID": client_id}
        ))
        assert response.status_code == 200

        data = response.json()
        assert data["client_id"] == client_id
        assert data["limit"] == 5
        assert data["remaining"] == 3
        assert data["request_count"] == 2
        assert data["window_start"] > 0
        assert data["reset"] > 0

        # Make another request - should be 3rd
        response = self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": client_id}
        ))
        assert response.status_code == 200
        assert response.json()["rate_limit"]["remaining"] == 2

    def test_status_endpoint_new_client(self):
        """Test status endpoint for client with no requests."""
        response = self._run_async(self._make_request(
            "GET", "/api/rate-limit/status", headers={"X-Client-ID": "new_client"}
        ))

        assert response.status_code == 200
        data = response.json()
        assert data["client_id"] == "new_client"
        assert data["limit"] == 5
        assert data["remaining"] == 5
        assert data["request_count"] == 0

    def test_missing_client_id_fallbacks_to_ip(self):
        """Test that missing X-Client-ID falls back to IP."""
        response = self._run_async(self._make_request("GET", "/api/test"))

        assert response.status_code == 200
        # Should use IP-based client ID
        data = response.json()
        assert data["message"] == "Request allowed"

    def test_empty_client_id_returns_400(self):
        """Test that empty X-Client-ID returns 400."""
        response = self._run_async(self._make_request(
            "GET", "/api/test", headers={"X-Client-ID": "   "}
        ))

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "Invalid client identifier" in data["detail"]

    def test_invalid_endpoint_returns_404(self):
        """Test that invalid endpoint returns 404."""
        response = self._run_async(self._make_request("GET", "/api/nonexistent"))

        assert response.status_code == 404