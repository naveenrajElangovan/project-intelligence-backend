from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.entra import EntraTokenValidator
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_identity_access_store
from app.authorization.sql_store import SqlIdentityAccessStore
from app.config import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def get_entra_token_validator() -> EntraTokenValidator:
    return EntraTokenValidator(get_settings())


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    validator: Annotated[EntraTokenValidator, Depends(get_entra_token_validator)],
    access_store: Annotated[SqlIdentityAccessStore, Depends(get_identity_access_store)],
) -> EntraPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A bearer access token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = await validator.validate(credentials.credentials)
    await access_store.record_authentication(principal)
    return principal


CurrentPrincipal = Annotated[EntraPrincipal, Depends(get_current_principal)]
