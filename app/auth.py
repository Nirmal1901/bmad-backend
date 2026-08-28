"""
Real login for BMad Studio: username + password, JWT bearer session.

Kept deliberately small — one secret, one algorithm, one token
lifetime — since this still runs as a single small FastAPI service.
Swap BMAD_JWT_SECRET for a real secret (env var) before any shared
deployment; the fallback here is only for local/dev use.
"""
import os
import datetime

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.database import get_db
from app import models

SECRET_KEY = os.environ.get("BMAD_JWT_SECRET", "dev-only-secret-change-me")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24 * 14  # 2 weeks

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _role_value(user: models.User) -> str:
    return user.role.value if hasattr(user.role, "value") else user.role


def is_admin(user: models.User) -> bool:
    return _role_value(user) == "admin"


def create_access_token(user: models.User) -> str:
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": _role_value(user),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_EXPIRE_HOURS),
        "iat": datetime.datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired — please log in again")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid session token")


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> models.User:
    if credentials is None:
        raise HTTPException(401, "Not authenticated — please log in")
    payload = _decode(credentials.credentials)
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(401, "Invalid session token")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(401, "Account no longer exists")
    return user


def get_current_admin(current_user: models.User = Depends(get_current_user)) -> models.User:
    if not is_admin(current_user):
        raise HTTPException(403, "Admin access required")
    return current_user
