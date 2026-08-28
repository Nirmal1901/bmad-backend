from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.bmad_loader import sync_agents_to_db
from app.auth import get_current_user, get_current_admin

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/sync", response_model=list[schemas.AgentOut])
def sync_agents(db: Session = Depends(get_db), admin: models.User = Depends(get_current_admin)):
    """Re-scan the _bmad folder and upsert discovered agents into the DB.
    Call this once at startup (already done automatically) or after
    dropping in a new/updated _bmad folder. Admin-only — this rewrites
    shared agent definitions everyone's pipelines reference."""
    sync_agents_to_db(db)
    return db.query(models.Agent).all()


@router.get("", response_model=list[schemas.AgentOut])
def list_agents(module: str | None = None, db: Session = Depends(get_db),
                 current_user: models.User = Depends(get_current_user)):
    q = db.query(models.Agent)
    if module:
        q = q.filter(models.Agent.module == module)
    return q.all()


@router.get("/{agent_id}", response_model=schemas.AgentOut)
def get_agent(agent_id: str, db: Session = Depends(get_db),
               current_user: models.User = Depends(get_current_user)):
    agent = db.query(models.Agent).filter(models.Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(404, f"Agent '{agent_id}' not found")
    return agent
