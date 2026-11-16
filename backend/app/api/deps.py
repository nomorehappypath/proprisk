# FastAPI dependencies for the one fixed local office and owner. PropRisk
# has no sign-in: every request is the same local owner, found by the fixed
# ids the baseline migration creates (app/core/local_identity.py).
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.local_identity import LOCAL_TENANT_ID, LOCAL_USER_ID
from app.db import get_db
from app.models import Tenant, User

NOT_FOUND = "We couldn't find that."


def current_user(db: Session = Depends(get_db)) -> User:
    """Always the one local owner row — no header is read or checked."""
    user = db.get(User, LOCAL_USER_ID)
    if user is None:
        raise HTTPException(status_code=500, detail="The local owner row is missing.")
    return user


def current_tenant(db: Session = Depends(get_db)) -> Tenant:
    """Always the one local office row."""
    tenant = db.get(Tenant, LOCAL_TENANT_ID)
    if tenant is None:
        raise HTTPException(status_code=500, detail="The local office row is missing.")
    return tenant


def plain_error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)
