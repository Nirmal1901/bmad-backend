from fastapi import APIRouter, UploadFile, File, Form, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/brd", tags=["brd"])


@router.post("/upload", response_model=schemas.PipelineOut)
async def upload_brd(
    name: str = Form("Untitled Pipeline"),
    file: UploadFile | None = File(None),
    text: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Accepts either an uploaded BRD file (txt/md) or pasted text, and
    creates a new empty pipeline (no nodes yet) holding that BRD, owned
    by whoever's logged in. Agents get added afterward via
    POST /pipelines/{id}/nodes."""
    brd_text = text or ""
    if file is not None:
        raw = await file.read()
        brd_text = raw.decode("utf-8", errors="ignore")

    pipeline = models.Pipeline(name=name, brd_text=brd_text, owner_id=current_user.id)
    db.add(pipeline)
    db.commit()
    db.refresh(pipeline)
    return pipeline
