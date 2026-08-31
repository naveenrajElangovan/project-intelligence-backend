from functools import lru_cache

from app.authorization.graph import GraphProjectAccessReader
from app.authorization.sql_store import SqlIdentityAccessStore
from app.config import get_settings
from app.infrastructure.sql.database import get_session_factory


def get_identity_access_store() -> SqlIdentityAccessStore:
    return SqlIdentityAccessStore(get_session_factory())


@lru_cache
def get_graph_project_access_reader() -> GraphProjectAccessReader:
    return GraphProjectAccessReader(get_settings(), access_store=get_identity_access_store())
