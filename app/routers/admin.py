"""
Admin-only views: every account, and every session (pipeline) across
every user, with the ability to delete either. Gated by role="admin"
on the JWT-verified user — see app/auth.get_current_admin.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[schemas.UserOut])
def list_users(db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin)):
    return db.query(models.User).order_by(models.User.created_at.desc()).all()


@router.put("/users/{user_id}/role")
def set_user_role(
    user_id: int, role: str,
    db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin),
):
    if role not in ("admin", "user"):
        raise HTTPException(400, "role must be 'admin' or 'user'")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.role = models.UserRole(role)
    db.commit()
    return {"id": user_id, "role": role}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "You can't delete the account you're currently logged in as")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    db.delete(user)
    db.commit()
    return {"deleted": user_id}


@router.get("/sessions", response_model=list[schemas.PipelineOut])
def list_all_sessions(db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin)):
    """Every pipeline/session across every user — BRD, nodes, artifacts,
    epics are all reachable from here the same way a normal owner
    reaches their own via GET /pipelines/{id}."""
    return db.query(models.Pipeline).order_by(models.Pipeline.updated_at.desc()).all()


@router.delete("/sessions/{pipeline_id}")
def delete_any_session(
    pipeline_id: int,
    db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin),
):
    pipeline = db.query(models.Pipeline).filter(models.Pipeline.id == pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, "Session not found")
    db.delete(pipeline)
    db.commit()
    return {"deleted": pipeline_id}
