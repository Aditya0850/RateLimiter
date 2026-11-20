# Rate Limiter API

A production-ready Fixed Window Counter rate limiting service built with **FastAPI**, **MongoDB**, and **Python 3.14+**.

## Project Overview

This project implements a rate limiter as part of the **Unstructured Database (CSE13202)** college assignment. It provides a backend API that limits the number of requests a client can make within a specified time window.

### Problem Statement

Web services need to protect against abuse, ensure fair usage, and maintain stability under high load. A rate limiter enforces request quotas per client, returning HTTP 429 (Too Many Requests) when limits are exceeded.

### Features

- **Fixed Window Counter Algorithm** - Deterministic time buckets with atomic operations
- **Independent Client Limits** - Each client (identified by `X-Client-ID` header) has separate counters
- **Standard Rate Limit Headers** - `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, `Retry-After`
- **Status Endpoint** - Check quota without consuming a request (`GET /api/rate-limit/status`)
- **MongoDB Persistence** - Atomic operations with TTL-based cleanup
- **Concurrency Safe** - Handles 100+ simultaneous requests correctly
- **Comprehensive Tests** - 37 tests covering unit, integration, and concurrency scenarios

---

## Architecture

```
FastAPI Application
├── Routes (app/routes.py)
│   ├── GET /                  -> Service info
│   ├── GET /health            -> Database health check
│   ├── GET /api/test          -> Rate limited test endpoint
│   └── GET /api/rate-limit/status -> Quota status (no consume)
├── Rate Limiter Engine (app/limiter.py)
│   ├── Fixed Window Counter with atomic MongoDB operations
│   ├── Application-level expiry validation (never rely on TTL)
│   └── DuplicateKeyError retry for concurrent upserts
├── Database Layer (app/database.py)
│   ├── Async Motor client with connection pooling
│   ├── Index management (unique compound + TTL)
│   └── Lifespan management
└── MongoDB Community Server
    Database: rate_limiter_db
    Collection: rate_limits
    Indexes:
    ├── unique_client_window (client_id, window_start) - UNIQUE
    └── ttl_expires_at (expires_at) - TTL 0s
```

---

## Fixed Window Counter Algorithm

### How It Works

```
Time ------------------------------------------------>

Window 1 (0-60s)    |    Window 2 (60-120s)   |    Window 3 (120-180s)
[##########]       |    [..........]         |    [..........]
  5/5 requests        0/5 requests            0/5 requests
  window_start=0      window_start=60         window_start=120
```

### Window Calculation

```python
# Deterministic Unix time buckets
window_seconds = 60
now = time.time()  # e.g., 1732036830.5
window_start = int(now // window_seconds) * window_seconds  # 1732036800
expires_at = window_start + window_seconds  # 1732036860
```

All timestamps in `[1732036800, 1732036860)` map to `window_start=1732036800`.

### Key Properties

| Property | Behavior |
|----------|----------|
| Deterministic | Same timestamp always maps to same window |
| Fixed Boundaries | Windows align to Unix epoch (not request time) |
| Independent Clients | Each client_id has separate counter per window |
| Atomic Increments | MongoDB $inc with upsert prevents race conditions |
| Application Expiry | Validates expires_at on every request (TTL is async) |

---

## MongoDB Database Design

### Document Structure

```json
{
  "client_id": "user_101",
  "request_count": 3,
  "window_start": ISODate("2026-11-19T16:41:00Z"),
  "expires_at": ISODate("2026-11-19T16:42:00Z")
}
```

| Field | Type | Description |
|-------|------|-------------|
| client_id | String | Unique client identifier (from X-Client-ID header) |
| request_count | Int32 | Number of requests in current window |
| window_start | BSON Date (UTC) | Window start timestamp |
| expires_at | BSON Date (UTC) | Window expiry = window_start + 60s |

### Indexes

#### 1. Unique Compound Index: unique_client_window

```javascript
db.rate_limits.createIndex(
  { "client_id": 1, "window_start": 1 },
  { unique: true, name: "unique_client_window" }
)
```

**Purpose:** Ensures exactly ONE document per client per time window.
- Prevents duplicate documents during concurrent upserts
- Enforces single counter per client per window
- Critical for correctness under high concurrency

#### 2. TTL Index: ttl_expires_at

```javascript
db.rate_limits.createIndex(
  { "expires_at": 1 },
  { expireAfterSeconds: 0, name: "ttl_expires_at" }
)
```

**Purpose:** Automatic cleanup of expired window documents.
- MongoDB removes documents when expires_at passes
- **Important:** TTL deletion is asynchronous (~60s delay)
- Application **MUST** validate expires_at on every request
- Never rely on TTL alone for rate limit enforcement

---

## Installation & Setup

### Prerequisites

- **Python 3.14+**
- **MongoDB Community Server 8.0+** (running on `localhost:27017`)
- **MongoDB Compass** (optional, for inspection)

### MongoDB Community Server Setup

1. **Download & Install:**
   - Windows: [MongoDB Community Server](https://www.mongodb.com/try/download/community)
   - Run as Windows Service (default)

2. **Verify Installation:**
   ```bash
   net start MongoDB
   # Or check service: "MongoDB Server (MongoDB)"
   ```

3. **MongoDB Compass (GUI):**
   - Connect to `mongodb://localhost:27017`
   - Database: `rate_limiter_db`
   - Collection: `rate_limits`

### Project Setup

```bash
# Clone/navigate to project
cd A:\Projects\RateLimiter

# Virtual environment already exists
# Activate it:
.\venv\Scripts\activate

# Verify dependencies
python -c "import fastapi, pymongo, motor, pydantic; print('OK')"
```

### Environment Configuration

Create `.env` file (optional, uses defaults):

```env
# MongoDB
MONGODB_URI=mongodb://localhost:27017
MONGODB_DATABASE=rate_limiter_db
MONGODB_COLLECTION=rate_limits

# Rate Limiter
RATE_LIMIT_MAX_REQUESTS=5
RATE_LIMIT_WINDOW_SECONDS=60

# Server
HOST=0.0.0.0
PORT=8000
RELOAD=false

# Security
TRUSTED_PROXY_COUNT=0

# Timeouts
REQUEST_TIMEOUT_SECONDS=30.0
MONGODB_TIMEOUT_MS=5000
```

---

## Running the Application

### Start Server

```bash
# Development (with auto-reload)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Server starts at: `http://localhost:8000`

### API Documentation

- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`
- **OpenAPI JSON:** `http://localhost:8000/openapi.json`

---

## API Endpoints

### 1. Root - Service Information

```bash
GET /
```

**Response (200):**
```json
{
  "service": "Rate Limiter API",
  "version": "1.0.0",
  "description": "Fixed Window Counter rate limiting service"
}
```

### 2. Health Check

```bash
GET /health
```

**Response (200):**
```json
{
  "status": "ok",
  "database": "connected"
}
```

### 3. Test Endpoint (Rate Limited)

```bash
GET /api/test
Header: X-Client-ID: user_101
```

**Success Response (200):**
```json
{
  "message": "Request allowed",
  "rate_limit": {
    "limit": 5,
    "remaining": 4,
    "reset": 1732036860,
    "retry_after": null
  }
}
```

**Headers:**
```
RateLimit-Limit: 5
RateLimit-Remaining: 4
RateLimit-Reset: 1732036860
```

**Rate Limited Response (429):**
```json
{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded",
  "detail": {
    "limit": 5,
    "remaining": 0,
    "reset_at": 1732036860,
    "retry_after": 45
  }
}
```

**Headers:**
```
RateLimit-Limit: 5
RateLimit-Remaining: 0
RateLimit-Reset: 1732036860
Retry-After: 45
```

### 4. Rate Limit Status (No Consume)

```bash
GET /api/rate-limit/status
Header: X-Client-ID: user_101
```

**Response (200):**
```json
{
  "client_id": "user_101",
  "limit": 5,
  "remaining": 3,
  "reset": 1732036860,
  "request_count": 2,
  "window_start": 1732036800
}
```

> **Note:** This endpoint does NOT consume a request. Use it to check quota before making rate-limited calls.

---

## Testing

### Run All Tests

```bash
# All tests (unit + integration + concurrency + routes)
python -m pytest tests/ -v

# Specific test suites
python -m pytest tests/test_limiter.py -v      # Unit tests (mocked DB)
python -m pytest tests/test_integration.py -v  # Integration tests (real MongoDB)
python -m pytest tests/test_concurrency.py -v  # Concurrency tests
python -m pytest tests/test_routes.py -v       # API endpoint tests
```

### Test Coverage

| Test Suite | Tests | Coverage |
|------------|-------|----------|
| Unit (mocked) | 9 | Rate limiter logic, edge cases |
| Integration (real MongoDB) | 11 | Full API with real database |
| Concurrency | 6 | 20-100 simultaneous requests |
| API Routes | 11 | All endpoints, headers, errors |
| **Total** | **37** | **All requirements verified** |

### Key Test Scenarios

- First request succeeds
- 5 requests allowed, 6th rejected (429)
- Different clients have independent counters
- Counter resets after window expiration
- Remaining count is accurate
- Reset timestamp is correct
- MongoDB documents use BSON Date (UTC)
- TTL index exists on expires_at
- Unique compound index on (client_id, window_start)
- 20 concurrent requests -> exactly 5 allowed
- 100 concurrent requests -> rate limit enforced
- DuplicateKeyError handled with retry
- Status endpoint doesn't consume requests

---

## Security Considerations

| Measure | Implementation |
|---------|----------------|
| No Hardcoded Secrets | All config via environment variables |
| Input Validation | Empty/whitespace X-Client-ID rejected (400) |
| Header-Based ID | Uses X-Client-ID header for explicit client identification; falls back to IP if header absent |
| Trusted Proxy Config | TRUSTED_PROXY_COUNT=0 (no proxy by default) |
| Request Timeouts | 30s HTTP, 5s MongoDB connection |
| No Sensitive Logs | No client IDs or internal errors in logs |
| Safe Error Handling | Generic 503 for DB errors, no stack traces |
| CORS | Wildcard for dev (configure for production) |

### Production Deployment Checklist

- [ ] Set TRUSTED_PROXY_COUNT if behind load balancer
- [ ] Configure CORS origins explicitly
- [ ] Use MongoDB authentication (username/password)
- [ ] Enable TLS for MongoDB connection
- [ ] Set RELOAD=false
- [ ] Configure proper logging (JSON format, log aggregation)
- [ ] Set up monitoring/alerting for rate limit events

---

## Inspecting with MongoDB Compass

### Connect
1. Open MongoDB Compass
2. Connection: `mongodb://localhost:27017`
3. Database: `rate_limiter_db`
4. Collection: `rate_limits`

### View Documents
- Documents show client_id, request_count, window_start, expires_at
- Timestamps displayed in local time (stored as UTC)

### Check Indexes
1. Select collection -> **Indexes** tab
2. Verify:
   - unique_client_window - Unique, Compound (client_id: 1, window_start: 1)
   - ttl_expires_at - TTL, Single (expires_at: 1), Expire After: 0s

### Query Examples

```javascript
// All documents for a client
{ "client_id": "user_101" }

// Current window documents
{ "window_start": ISODate("2026-11-19T16:41:00Z") }

// Expired documents (should be auto-removed by TTL)
{ "expires_at": { $lt: ISODate("2026-11-19T16:42:00Z") } }
```

---

## Known Limitations

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| Fixed Window Burst | Up to 2x limit at window boundaries | Acceptable for most use cases; use Sliding Window for stricter limits |
| TTL Async Cleanup | Expired documents may persist ~60s after expiry | Application validates expires_at on every request |
| Single MongoDB Instance | No HA/replication | Deploy MongoDB replica set for production |
| Memory Growth | High-cardinality clients create many documents | TTL cleans up; monitor collection size |
| No Distributed Lock | Relies on MongoDB atomicity | Sufficient for single MongoDB; use Redis for multi-region |

---

## Project Structure

```
RateLimiter/
├── app/
│   ├── __init__.py          # Package marker
│   ├── main.py              # FastAPI app factory & lifespan
│   ├── config.py            # Settings (Pydantic BaseSettings)
│   ├── database.py          # MongoDB connection & indexes
│   ├── limiter.py           # Rate limiter engine (Fixed Window)
│   ├── models.py            # Pydantic request/response models
│   ├── routes.py            # API endpoints
│   └── exceptions.py        # Custom exceptions
├── tests/
│   ├── __init__.py
│   ├── test_limiter.py      # Unit tests (mocked DB)
│   ├── test_integration.py  # Integration tests (real MongoDB)
│   ├── test_concurrency.py  # Concurrency stress tests
│   └── test_routes.py       # API endpoint tests
├── .env.example             # Environment template
├── .gitignore
├── requirements.txt         # Python dependencies
└── README.md               # This file
```

---

## Academic Context

This project was developed for **CSE13202 - Unstructured Database** course.

**Learning Outcomes Demonstrated:**
- MongoDB document modeling with BSON Date types
- Index design (unique compound + TTL)
- Atomic operations (find_one_and_update with upsert)
- Concurrency control in NoSQL databases
- TTL indexes for automatic data expiration
- Async Python with Motor driver
- FastAPI dependency injection & validation
- Comprehensive testing (unit, integration, concurrency)
- Security best practices for API development

---

## License

Academic project - CSE13202 Unstructured Database Assignment.

---

*Built with FastAPI, MongoDB, and Python 3.14+*