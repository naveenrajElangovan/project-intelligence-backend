from functools import lru_cache

from app.config import get_settings
from app.infrastructure.sql.database import get_session_factory
from app.infrastructure.sql.project_repository import SqlProjectRepository


@lru_cache
def get_project_store() -> SqlProjectRepository:
    get_settings()
    return SqlProjectRepository(get_session_factory())
