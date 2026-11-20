"""MongoDB database connection and index management."""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING
from pymongo.errors import ConnectionFailure, OperationFailure, DuplicateKeyError

from app.config import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages MongoDB connection lifecycle and index initialization."""

    def __init__(self) -> None:
        self._client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AsyncIOMotorDatabase] = None

    @property
    def client(self) -> AsyncIOMotorClient:
        if self._client is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._client

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._db

    async def initialize(self) -> None:
        """Initialize MongoDB connection and create indexes."""
        if self._client is not None:
            logger.warning("Database already initialized")
            return

        logger.info(f"Connecting to MongoDB at {settings.mongodb_uri}")
        self._client = AsyncIOMotorClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
            connectTimeoutMS=settings.mongodb_timeout_ms,
            maxPoolSize=10,
            minPoolSize=1,
        )
        self._db = self._client[settings.mongodb_database]

        # Verify connection
        try:
            await self._client.admin.command("ping")
            logger.info("MongoDB connection verified")
        except ConnectionFailure as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            await self.close()
            raise

        # Initialize indexes
        await self._create_indexes()

    async def _create_indexes(self) -> None:
        """Create required indexes for the rate_limits collection."""
        collection = self._db[settings.mongodb_collection]

        # 1. Unique compound index on (client_id, window_start)
        # Ensures exactly one counter document per client per time window.
        # Prevents duplicate documents during concurrent upserts.
        try:
            await collection.create_index(
                [("client_id", ASCENDING), ("window_start", ASCENDING)],
                unique=True,
                name="unique_client_window",
            )
            logger.info("Created unique compound index: (client_id, window_start)")
        except OperationFailure as e:
            logger.error(f"Failed to create unique compound index: {e}")
            raise

        # 2. TTL index on expires_at
        # Automatically removes expired window documents.
        # Note: TTL deletion is asynchronous (~60s delay). Application logic
        # MUST validate expires_at for correctness; never rely on TTL alone.
        try:
            await collection.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="ttl_expires_at",
            )
            logger.info("Created TTL index on expires_at")
        except OperationFailure as e:
            logger.error(f"Failed to create TTL index: {e}")
            raise

        # Verify indexes exist
        indexes = await collection.list_indexes().to_list(length=None)
        index_names = [idx.get("name") for idx in indexes]
        logger.info(f"Current indexes: {index_names}")

        required = {"unique_client_window", "ttl_expires_at"}
        missing = required - set(index_names)
        if missing:
            raise RuntimeError(f"Missing required indexes: {missing}")

    async def close(self) -> None:
        """Close MongoDB connection."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
            logger.info("MongoDB connection closed")

    async def health_check(self) -> bool:
        """Check if database connection is healthy."""
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            return False


# Global database manager instance
db_manager = DatabaseManager()


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan context manager for database connection."""
    await db_manager.initialize()
    try:
        yield
    finally:
        await db_manager.close()


def get_database() -> AsyncIOMotorDatabase:
    """FastAPI dependency to get database instance."""
    return db_manager.db