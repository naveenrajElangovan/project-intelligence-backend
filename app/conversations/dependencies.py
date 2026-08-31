from functools import lru_cache

from app.config import get_settings
from app.conversations.store import MongoConversationStore


@lru_cache
def get_conversation_store() -> MongoConversationStore:
    """Create the process-wide Mongo conversation store from validated settings."""

    settings = get_settings()
    return MongoConversationStore(
        settings.chat_mongodb_url,
        settings.chat_mongodb_database,
        retention_days=settings.chat_retention_days,
        history_turns=settings.chat_history_turns,
    )
