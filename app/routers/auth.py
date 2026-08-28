"""
Real accounts: register, login, and the profile bits that used to live
on the old no-password Profile model (name/about/agent_role — which
agents show up in the canvas). The very first account ever created
becomes admin automatically so there's always at least one, with no
separate bootstrap step.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])

VALID_AGENT_ROLES = {"pm", "developer"}


@router.post("/register", response_model=schemas.TokenOut)
def register(payload: schemas.UserRegisterIn, db: Session = Depends(get_db)):
    username = payload.username.strip().lower()
    if not username:
        raise HTTPException(400, "username is required")
    if not payload.password or len(payload.password) < 4:
        raise HTTPException(400, "password must be at least 4 characters")
    if db.query(models.User).filter(models.User.username == username).first():
        raise HTTPException(400, f"username '{username}' is already taken")

    agent_role = payload.agent_role if payload.agent_role in VALID_AGENT_ROLES else "developer"
    is_first_account = db.query(models.User).count() == 0

    user = models.User(
        username=username,
        password_hash=hash_password(payload.password),
        role=models.UserRole.admin if is_first_account else models.UserRole.user,
        name=(payload.name or username).strip(),
        about=payload.about,
        agent_role=agent_role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return schemas.TokenOut(access_token=create_access_token(user), user=user)


@router.post("/login", response_model=schemas.TokenOut)
def login(payload: schemas.UserLoginIn, db: Session = Depends(get_db)):
    username = payload.username.strip().lower()
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")
    user.last_active_at = datetime.datetime.utcnow()
    db.commit()
    return schemas.TokenOut(access_token=create_access_token(user), user=user)


@router.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


@router.put("/me", response_model=schemas.UserOut)
def update_me(
    payload: schemas.UserUpdateIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if payload.name is not None:
        current_user.name = payload.name.strip() or current_user.name
    if payload.about is not None:
        current_user.about = payload.about
    if payload.agent_role is not None:
        if payload.agent_role not in VALID_AGENT_ROLES:
            raise HTTPException(400, f"agent_role must be one of {sorted(VALID_AGENT_ROLES)}")
        current_user.agent_role = payload.agent_role
    db.commit()
    db.refresh(current_user)
    return current_user
