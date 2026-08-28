"""
Lightweight local profiles — no password, no session token. This app
runs single-user on SQLite; a "profile" is just who's currently using
it, so the UI can show the right set of agents (PM sees artifact
agents only; Developer sees everything) and remember who's who across
restarts. Not real auth — don't put anything sensitive behind this.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas

router = APIRouter(prefix="/profiles", tags=["profiles"])

VALID_ROLES = {"pm", "developer"}


def _validate_role(role: str) -> str:
    if role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(VALID_ROLES)}, got {role!r}")
    return role


@router.get("", response_model=list[schemas.ProfileOut])
def list_profiles(db: Session = Depends(get_db)):
    return db.query(models.Profile).order_by(models.Profile.last_active_at.desc()).all()


@router.post("", response_model=schemas.ProfileOut)
def create_profile(payload: schemas.ProfileIn, db: Session = Depends(get_db)):
    _validate_role(payload.role)
    if not payload.name.strip():
        raise HTTPException(400, "name is required")
    profile = models.Profile(name=payload.name.strip(), about=payload.about, role=payload.role)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/{profile_id}", response_model=schemas.ProfileOut)
def get_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@router.put("/{profile_id}", response_model=schemas.ProfileOut)
def update_profile(profile_id: int, payload: schemas.ProfileIn, db: Session = Depends(get_db)):
    _validate_role(payload.role)
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    profile.name = payload.name.strip() or profile.name
    profile.about = payload.about
    profile.role = payload.role
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/{profile_id}/activate", response_model=schemas.ProfileOut)
def activate_profile(profile_id: int, db: Session = Depends(get_db)):
    """Just bumps last_active_at so the switcher can sort 'recently
    used' profiles to the top. Actual "who's active" state lives
    client-side (localStorage) — this is single-user local software,
    not a multi-tenant server."""
    import datetime
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    profile.last_active_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/{profile_id}")
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    db.delete(profile)
    db.commit()
    return {"ok": True}
