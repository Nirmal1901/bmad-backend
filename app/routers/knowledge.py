"""
Admin-facing Knowledge Base endpoints: upload Regulatory Compliance
Requirements, Dev/Testing Guidelines, Data Glossary (or custom) docs
that become available, via RAG, to every agent whose role matches
the doc's applicable_roles. See app/knowledge_base.py for the
chunk/embed/retrieve implementation, and pipelines.py's node-scoped
/documents endpoints for the per-node "attach just to this agent"
counterpart.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.database import get_db
from app import schemas, models, knowledge_base as kb
from app.auth import get_current_user, get_current_admin

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


@router.post("/documents", response_model=schemas.DocumentOut)
async def upload_global_document(
    file: UploadFile = File(...),
    category: str = Form("custom"),
    title: Optional[str] = Form(None),
    applicable_roles: str = Form('["pm", "developer"]'),  # JSON list as a form field
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_admin),
):
    if category not in kb.CATEGORIES:
        raise HTTPException(400, f"category must be one of {list(kb.CATEGORIES)}")

    try:
        roles = json.loads(applicable_roles)
        if not isinstance(roles, list) or not roles:
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, 'applicable_roles must be a JSON list, e.g. ["pm","developer"]')

    raw_bytes = await file.read()
    try:
        text = kb.extract_text(file.filename, raw_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        doc = kb.ingest_document(
            db,
            title=title or file.filename,
            category=category,
            scope="global",
            raw_text=text,
            source_filename=file.filename,
            applicable_roles=roles,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return doc


@router.get("/documents", response_model=list[schemas.DocumentOut])
def list_global_documents(category: Optional[str] = None, db: Session = Depends(get_db),
                            current_user: models.User = Depends(get_current_user)):
    docs = kb.list_documents(db, scope="global")
    if category:
        docs = [d for d in docs if d.category == category]
    return docs


@router.delete("/documents/{doc_id}")
def delete_global_document(doc_id: int, db: Session = Depends(get_db),
                             admin: models.User = Depends(get_current_admin)):
    doc = db.query(models.Document).filter(
        models.Document.id == doc_id, models.Document.scope == "global"
    ).first()
    if not doc:
        raise HTTPException(404, "Global document not found")
    kb.delete_document(db, doc_id)
    return {"deleted": True, "id": doc_id}


@router.get("/categories")
def list_categories(current_user: models.User = Depends(get_current_user)):
    """So the frontend upload form can populate its category dropdown
    without hardcoding the list twice."""
    return kb.CATEGORIES
