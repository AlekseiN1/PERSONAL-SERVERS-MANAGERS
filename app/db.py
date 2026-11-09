"""MongoDB connection (motor, async)."""
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from app.config import settings

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongo_uri)
    return _client


def get_servers_collection() -> AsyncIOMotorCollection:
    return get_client()[settings.mongo_db]["servers"]


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
