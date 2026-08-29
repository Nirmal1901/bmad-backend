import yaml
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fastapi import UploadFile, File, Form
from typing import Optional

from app.database import get_db, SessionLocal
from app import models, schemas
from app.llm_router import llm_router
from app import knowledge_base as kb
from app.auth import get_current_user, is_admin

logger = logging.getLogger("bmad_studio.pipelines")

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


# ---------- helpers ----------

def _get_pipeline_or_404(
    db: Session, pipeline_id: int, current_user: "models.User | None" = None
) -> models.Pipeline:
    """Fetch a pipeline (a 'session' in the UI) and, whenever a user is
    supplied, enforce that only its owner or an admin can touch it.
    current_user defaults to None only for internal/background callers
    (the execution worker thread) that don't have a request user."""
    pipeline = db.query(models.Pipeline).filter(
        models.Pipeline.id == pipeline_id
    ).first()
    if not pipeline:
        raise HTTPException(404, f"Pipeline {pipeline_id} not found")
    if current_user is not None and not is_admin(current_user):
        if pipeline.owner_id is not None and pipeline.owner_id != current_user.id:
            raise HTTPException(403, "You don't have access to this session")
    return pipeline


def _renumber(pipeline: models.Pipeline, db: Session):
    """Keep order_index contiguous (0, 1, 2, ...) after add/remove/reorder."""
    nodes = sorted(pipeline.nodes, key=lambda n: n.order_index)
    for i, n in enumerate(nodes):
        n.order_index = i
    db.commit()


def _cascade_stale(pipeline: models.Pipeline, from_index: int, db: Session) -> list[int]:
    """Mark every node with order_index > from_index as stale.
    Returns the list of node ids that were flagged."""
    stale_ids = []
    for n in pipeline.nodes:
        if n.order_index > from_index and n.status != models.NodeStatus.not_run:
            n.status = models.NodeStatus.stale
            stale_ids.append(n.id)
    db.commit()
    return stale_ids


def _pipeline_mode_directive(selected_menu_item: str | None) -> str:
    if selected_menu_item:
        deliverable_instruction = (
            f'- The user has explicitly picked which of your menu items to run: '
            f'"{selected_menu_item}". Execute exactly that one — do not substitute '
            f"a different item even if another seems like a closer fit for the input."
        )
    else:
        deliverable_instruction = (
            "- Treat the input below as if the user had already selected the menu\n"
            '  item that best matches "analyze/process this input and produce your\n'
            '  primary deliverable for this role" (e.g. a Business Analyst produces\n'
            "  a requirements breakdown; a PM produces a PRD; an Architect produces\n"
            "  an architecture doc) — pick whichever of your own menu items is the\n"
            "  closest fit and execute it directly."
        )
    return f"""
---
AUTOMATED PIPELINE MODE — READ BEFORE ACTIVATING

You are being run as one step in an automated, non-interactive pipeline.
There is no human present to type a menu selection right now, so the
following overrides apply on top of your persona instructions above:

- Do NOT display your numbered menu and do NOT stop to wait for user
  input. Any "STOP and WAIT" / "do not execute automatically" activation
  rule is suspended for this run.
- Do NOT ask clarifying questions. If something is ambiguous, make the
  most reasonable expert assumption for someone in your role, note it
  briefly, and proceed.
- Skip the greeting. Go straight to doing the work.
{deliverable_instruction}
- The input below may contain the original BRD AND multiple upstream
  agents' outputs, each under its own "# Output from ..." heading. Read
  ALL of them — they are cumulative context, not alternatives. Your
  deliverable should build on everything provided, not just repeat or
  summarize the most recent section. If your role has nothing new to
  add beyond what's already there, that itself is a sign you should
  reconsider which of your menu items is the real closest fit.
- Do NOT narrate any of this. Never write things like "I'll select the
  closest menu item: [CA] Create Architecture", "reading the workflow
  file for [DS] Dev Story", "checking config.yaml", or any mention of
  menu codes, activation steps, or config-loading. That's internal
  bookkeeping — it means nothing to the person reading your output and
  makes it look broken. Silently do all of that, then output only the
  finished deliverable itself, starting directly with its title/heading.
- Your entire reply should BE the finished artifact/deliverable in
  clean markdown — headings, lists, tables as appropriate — not a
  description of what you could do next, and not a log of your own
  internal process.
---
"""


def _node_input_text(pipeline: models.Pipeline, node: models.PipelineNode) -> str:
    """Build the input handed to a node: the BRD plus EVERY upstream
    node's output so far, in order — not just the immediately preceding
    node. Downstream agents (e.g. a 3rd node) otherwise never see the
    original BRD or earlier agents' work, only whatever the node right
    before them produced, which is how a Dev/SM agent ends up "clueless"
    and just echoing the previous artifact back."""
    parts = []
    if pipeline.brd_text:
        parts.append(f"# Source BRD / Dev Info\n\n{pipeline.brd_text}")
    upstream = sorted(
        (n for n in pipeline.nodes if n.order_index < node.order_index and n.output_text),
        key=lambda n: n.order_index,
    )
    for n in upstream:
        parts.append(f"# Output from {n.agent_id} (step {n.order_index + 1})\n\n{n.output_text}")
    if not parts:
        parts.append("(No upstream input yet — provide a BRD or run an upstream node first.)")
    return "\n\n---\n\n".join(parts)


def _with_kb_context(node: models.PipelineNode, input_text: str) -> str:
    """Prepend any relevant Knowledge Base context — global admin docs
    (Regulatory/Testing/Glossary, role-filtered) plus anything attached
    directly to this node — ahead of the normal input. Retrieval query
    is the input itself, so it's driven by what this node is actually
    about to work on, not a fixed keyword list. Silently no-ops (and
    never raises) if the KB is empty or Chroma isn't reachable — a
    knowledge base outage shouldn't block a pipeline run."""
    try:
        roles = node.agent.suitable_roles if node.agent else ["pm", "developer"]
        context_block = kb.retrieve_context(
            input_text, node_roles=roles, node_id=node.id,
        )
    except Exception:
        logger.exception("[KB] retrieval failed for node=%s — continuing without KB context", node.id)
        return input_text
    if not context_block:
        return input_text
    return f"{context_block}\n\n---\n\n{input_text}"


# ---------- pipeline CRUD ----------

@router.post("", response_model=schemas.PipelineOut)
def create_pipeline(payload: schemas.PipelineCreateIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = models.Pipeline(name=payload.name, brd_text=payload.brd_text, owner_id=current_user.id)
    db.add(pipeline)
    db.flush()  # get pipeline.id

    for i, agent_id in enumerate(payload.agent_ids):
        agent = db.query(models.Agent).filter(models.Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(400, f"Unknown agent_id '{agent_id}'")
        db.add(models.PipelineNode(
            pipeline_id=pipeline.id,
            agent_id=agent_id,
            order_index=i,
            persona_md=agent.base_persona_md,
            status=models.NodeStatus.not_run,
        ))
    db.commit()
    db.refresh(pipeline)
    return pipeline


@router.get("", response_model=list[schemas.PipelineOut])
def list_pipelines(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """A user's own 'sessions' (BRD -> pipeline -> artifacts -> epics ->
    code, all keyed off this row). Admins see everyone's here too via
    /admin/sessions instead, so this endpoint stays scoped to 'mine'."""
    q = db.query(models.Pipeline)
    if not is_admin(current_user):
        q = q.filter(models.Pipeline.owner_id == current_user.id)
    return q.order_by(models.Pipeline.updated_at.desc()).all()


@router.get("/{pipeline_id}", response_model=schemas.PipelineOut)
def get_pipeline(pipeline_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return _get_pipeline_or_404(db, pipeline_id, current_user)


@router.delete("/{pipeline_id}")
def delete_pipeline(pipeline_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    db.delete(pipeline)
    db.commit()
    return {"deleted": pipeline_id}


# ---------- node management (linear v1: add / remove / reorder) ----------

@router.post("/{pipeline_id}/nodes", response_model=schemas.NodeOut)
def add_node(pipeline_id: int, payload: schemas.NodeAddIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    agent = db.query(models.Agent).filter(models.Agent.id == payload.agent_id).first()
    if not agent:
        raise HTTPException(400, f"Unknown agent_id '{payload.agent_id}'")

    insert_at = payload.order_index if payload.order_index is not None else len(pipeline.nodes)

    # shift existing nodes at/after insert_at
    for n in pipeline.nodes:
        if n.order_index >= insert_at:
            n.order_index += 1

    # Default position: caller can override afterwards via the position
    # endpoint (e.g. dropped at a specific canvas point), but give it a
    # sane fallback so it doesn't render stacked at (0,0) before that.
    default_x = payload.position_x if payload.position_x is not None else insert_at * 320.0
    default_y = payload.position_y if payload.position_y is not None else 0.0

    node = models.PipelineNode(
        pipeline_id=pipeline.id,
        agent_id=payload.agent_id,
        order_index=insert_at,
        persona_md=agent.base_persona_md,
        status=models.NodeStatus.not_run,
        position_x=default_x,
        position_y=default_y,
    )
    db.add(node)
    db.commit()
    _renumber(pipeline, db)
    db.refresh(node)
    return node


@router.delete("/{pipeline_id}/nodes/{node_id}")
def remove_node(pipeline_id: int, node_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")
    removed_index = node.order_index
    db.delete(node)
    db.commit()
    _cascade_stale(pipeline, removed_index - 1, db)
    _renumber(pipeline, db)
    return {"deleted": node_id}


@router.put("/{pipeline_id}/nodes/reorder", response_model=schemas.PipelineOut)
def reorder_node(pipeline_id: int, payload: schemas.NodeReorderIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """v1 is linear-only: reordering just changes order_index and
    flags everything from the earliest touched point onward as stale."""
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == payload.node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")

    old_index = node.order_index
    new_index = max(0, min(payload.new_index, len(pipeline.nodes) - 1))

    nodes_sorted = sorted(pipeline.nodes, key=lambda n: n.order_index)
    nodes_sorted.remove(node)
    nodes_sorted.insert(new_index, node)
    for i, n in enumerate(nodes_sorted):
        n.order_index = i
    db.commit()

    _cascade_stale(pipeline, min(old_index, new_index) - 1, db)
    db.refresh(pipeline)
    return pipeline


@router.put("/{pipeline_id}/nodes/{node_id}/position", response_model=schemas.NodeOut)
def update_node_position(pipeline_id: int, node_id: int, payload: schemas.NodePositionIn,
                          db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Purely visual — where the node sits on the free-form canvas.
    Doesn't touch order_index (that's still what drives the context
    chain) and never triggers a stale cascade."""
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")
    node.position_x = payload.x
    node.position_y = payload.y
    db.commit()
    db.refresh(node)
    return node


@router.put("/{pipeline_id}/nodes/{node_id}/persona", response_model=schemas.NodeOut)
def update_node_persona(pipeline_id: int, node_id: int, payload: schemas.NodePersonaUpdateIn,
                         db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")
    node.persona_md = payload.persona_md
    node.status = models.NodeStatus.stale if node.output_text else models.NodeStatus.not_run
    db.commit()
    db.refresh(node)
    return node


@router.put("/{pipeline_id}/nodes/{node_id}/task", response_model=schemas.NodeOut)
def update_node_task(pipeline_id: int, node_id: int, payload: schemas.NodeTaskIn,
                      db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Which of the agent's real menu items to run — the guided
    dropdown alternative to hand-editing the persona markdown. Null
    goes back to 'let the agent decide'."""
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")
    node.selected_menu_item = payload.menu_item
    node.status = models.NodeStatus.stale if node.output_text else models.NodeStatus.not_run
    db.commit()
    db.refresh(node)
    return node


@router.put("/{pipeline_id}/nodes/{node_id}/max-tokens", response_model=schemas.NodeOut)
def update_node_max_tokens(pipeline_id: int, node_id: int, payload: schemas.NodeMaxTokensIn,
                            db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Hard output-token ceiling for this node's LLM call. Null resets
    it to the router default (4096). This is a ceiling only — there's
    no API-level 'minimum length'; enforce a floor via persona wording
    (e.g. 'aim for at least 300 words') if needed."""
    if payload.max_tokens is not None and payload.max_tokens < 1:
        raise HTTPException(400, "max_tokens must be a positive integer")
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")
    node.max_tokens = payload.max_tokens
    db.commit()
    db.refresh(node)
    return node


# ---------- per-node knowledge base attachments ----------
# The "I'm building my BRD node and want to give it extra context"
# case: a doc scoped to exactly this node, RAG'd against only when
# THIS node runs — separate from the global admin KB in /kb.

@router.post("/{pipeline_id}/nodes/{node_id}/documents", response_model=schemas.DocumentOut)
async def attach_node_document(
    pipeline_id: int, node_id: int,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")

    raw_bytes = await file.read()
    try:
        text = kb.extract_text(file.filename, raw_bytes)
        doc = kb.ingest_document(
            db,
            title=title or file.filename,
            category="custom",
            scope="node",
            raw_text=text,
            node_id=node.id,
            pipeline_id=pipeline.id,
            source_filename=file.filename,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return doc


@router.get("/{pipeline_id}/nodes/{node_id}/documents", response_model=list[schemas.DocumentOut])
def list_node_documents(pipeline_id: int, node_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")
    return kb.list_documents(db, scope="node", node_id=node_id)


@router.delete("/{pipeline_id}/nodes/{node_id}/documents/{doc_id}")
def delete_node_document(pipeline_id: int, node_id: int, doc_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")
    doc = db.query(models.Document).filter(
        models.Document.id == doc_id, models.Document.node_id == node_id
    ).first()
    if not doc:
        raise HTTPException(404, "Document not attached to this node")
    kb.delete_document(db, doc_id)
    return {"deleted": True, "id": doc_id}


# ---------- run (manual trigger, per node.) ----------

@router.post("/{pipeline_id}/nodes/{node_id}/run", response_model=schemas.RunResultOut)
def run_node(pipeline_id: int, node_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    node = next((n for n in pipeline.nodes if n.id == node_id), None)
    if not node:
        raise HTTPException(404, "Node not found in this pipeline")

    node.status = models.NodeStatus.running
    db.commit()

    input_text = _with_kb_context(node, _node_input_text(pipeline, node))
    effective_persona = node.persona_md + _pipeline_mode_directive(node.selected_menu_item)
    node.last_input_text = input_text
    db.commit()

    logger.info(
        "\n%s\n[RUN] pipeline=%s node=%s agent=%s\n%s\nPERSONA (first 400 chars):\n%s\n%s\nINPUT (first 800 chars):\n%s\n%s",
        "=" * 90, pipeline_id, node_id, node.agent_id, "-" * 90,
        effective_persona[:400], "-" * 90, input_text[:800], "-" * 90,
    )

    try:
        output = llm_router.run(effective_persona, input_text, node.max_tokens)
    except Exception as e:
        node.status = models.NodeStatus.error
        node.output_text = f"ERROR: {e}"
        db.commit()
        logger.info("[RUN ERROR] pipeline=%s node=%s -> %s\n%s", pipeline_id, node_id, e, "=" * 90)
        raise HTTPException(500, f"LLM run failed: {e}")

    logger.info(
        "OUTPUT (first 800 chars):\n%s\n%s\n",
        output[:800], "=" * 90,
    )

    node.output_text = output
    node.status = models.NodeStatus.fresh
    db.commit()

    stale_ids = _cascade_stale(pipeline, node.order_index, db)

    artifact = models.Artifact(
        pipeline_id=pipeline.id,
        node_id=node.id,
        title=f"{node.agent_id} output",
        content_md=output,
    )
    db.add(artifact)
    db.commit()
    db.refresh(node)

    return schemas.RunResultOut(
        node=node, stale_node_ids=stale_ids, artifact_id=artifact.id
    )


@router.post("/{pipeline_id}/nodes/{node_id}/run-stream")
def run_node_stream(pipeline_id: int, node_id: int,
                     current_user: models.User = Depends(get_current_user)):
    """Same as /run, but streams the model's output as it's generated —
    NDJSON lines of {"type": "delta", "text": "..."}, ending with either
    {"type": "done", "node": {...}, "stale_node_ids": [...], "artifact_id": ...}
    or {"type": "error", "message": "..."}. The frontend reads this via
    fetch() + ReadableStream, not EventSource (this is a POST, and NDJSON
    is simpler to frame than SSE when we control both ends).

    Deliberately NOT using Depends(get_db): that session gets closed by
    FastAPI as soon as this function returns the StreamingResponse, but
    generate() below keeps running well after that — it's what actually
    streams the body. Touching request-scoped ORM objects (pipeline.nodes,
    etc.) after that point raises DetachedInstanceError. This uses its
    own session for the whole request+stream lifetime instead, closed
    explicitly once the generator is done."""
    import json
    from fastapi.responses import StreamingResponse

    db = SessionLocal()
    try:
        pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
        node = next((n for n in pipeline.nodes if n.id == node_id), None)
        if not node:
            raise HTTPException(404, "Node not found in this pipeline")

        node.status = models.NodeStatus.running
        db.commit()

        input_text = _with_kb_context(node, _node_input_text(pipeline, node))
        effective_persona = node.persona_md + _pipeline_mode_directive(node.selected_menu_item)
        node.last_input_text = input_text
        db.commit()
    except HTTPException:
        db.close()
        raise
    except Exception:
        db.close()
        raise

    logger.info(
        "\n%s\n[RUN-STREAM] pipeline=%s node=%s agent=%s\n%s\nPERSONA (first 400 chars):\n%s\n%s\nINPUT (first 800 chars):\n%s\n%s",
        "=" * 90, pipeline_id, node_id, node.agent_id, "-" * 90,
        effective_persona[:400], "-" * 90, input_text[:800], "-" * 90,
    )

    def generate():
        chunks: list[str] = []
        try:
            try:
                for delta in llm_router.run_stream(effective_persona, input_text, node.max_tokens):
                    chunks.append(delta)
                    yield json.dumps({"type": "delta", "text": delta}) + "\n"
            except Exception as e:
                node.status = models.NodeStatus.error
                node.output_text = f"ERROR: {e}"
                db.commit()
                logger.info("[RUN-STREAM ERROR] pipeline=%s node=%s -> %s\n%s",
                            pipeline_id, node_id, e, "=" * 90)
                yield json.dumps({"type": "error", "message": str(e)}) + "\n"
                return

            output = "".join(chunks)
            logger.info("OUTPUT (first 800 chars):\n%s\n%s\n", output[:800], "=" * 90)

            try:
                node.output_text = output
                node.status = models.NodeStatus.fresh
                db.commit()

                stale_ids = _cascade_stale(pipeline, node.order_index, db)

                artifact = models.Artifact(
                    pipeline_id=pipeline.id,
                    node_id=node.id,
                    title=f"{node.agent_id} output",
                    content_md=output,
                )
                db.add(artifact)
                db.commit()
                db.refresh(node)

                yield json.dumps({
                    "type": "done",
                    "node": schemas.NodeOut.model_validate(node).model_dump(mode="json"),
                    "stale_node_ids": stale_ids,
                    "artifact_id": artifact.id,
                }) + "\n"
            except Exception as e:
                # The model finished generating fine — this is a failure
                # saving that output (e.g. a transient SQLite lock under
                # concurrent polling). Roll back and tell the client
                # explicitly rather than letting the stream die mid-chunk,
                # which the browser reports as ERR_INCOMPLETE_CHUNKED_ENCODING
                # with no useful detail at all.
                db.rollback()
                logger.exception(
                    "[RUN-STREAM SAVE ERROR] pipeline=%s node=%s -> %s",
                    pipeline_id, node_id, e,
                )
                yield json.dumps({
                    "type": "error",
                    "message": f"Generated output but failed to save it: {e}",
                }) + "\n"
        finally:
            db.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ---------- artifacts ----------

@router.get("/{pipeline_id}/artifacts", response_model=list[schemas.ArtifactOut])
def list_artifacts(pipeline_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    _get_pipeline_or_404(db, pipeline_id, current_user)
    return db.query(models.Artifact).filter(
        models.Artifact.pipeline_id == pipeline_id
    ).order_by(models.Artifact.created_at.asc()).all()


# ---------- epics (real, individually-implementable units — see models.Epic) ----------

TASK_EXTRACTION_PERSONA = """You extract a clean list of individually-implementable
tasks/epics from a developer-facing document (a Dev Story, implementation
plan, or similar). The document may use any format — numbered "Task N",
"Phase N", checkboxes, headings, plain bullets, whatever the author chose.

Find every distinct unit of work a developer could pick up and implement
on its own. Ignore prose, rationale, and anything that isn't an actual
task (don't invent tasks that aren't there).

Respond with ONLY a JSON array, nothing else — no markdown fences, no
commentary. Each element: {"title": "short task title", "content": "the
full relevant detail for that task, copied/summarized from the doc"}.
If you find zero real tasks, respond with an empty array: []
"""


def _parse_json_array(text: str) -> list[dict]:
    """Parse a JSON array of {title, content} objects out of raw LLM
    output. Tolerant of two real-world failure modes instead of hard
    -erroring on either:

    1. Markdown fences / stray commentary around the array — stripped
       the same way as before.
    2. Truncated output (the array got cut off mid-string because the
       model hit its token ceiling before finishing) — rather than
       letting json.loads blow up on the whole payload with an
       "Unterminated string" error and returning nothing, we walk the
       array by hand, keep every syntactically complete {...} object
       up to the cut-off point, and only drop the final, incomplete
       one. A long Dev Story with N tasks then yields N-1 usable
       epics instead of zero.
    """
    import json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    start = cleaned.find("[")
    if start == -1:
        raise ValueError("no JSON array found in model output")

    # Try the fast path first: maybe it wasn't truncated at all.
    end = cleaned.rfind("]")
    if end != -1:
        try:
            data = json.loads(cleaned[start:end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass  # fall through to the tolerant brace-walker below

    body = cleaned[start + 1:]
    objects: list[str] = []
    depth = 0
    obj_start: Optional[int] = None
    in_string = False
    escape = False
    for i, ch in enumerate(body):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                objects.append(body[obj_start:i + 1])
                obj_start = None

    parsed = []
    for obj_text in objects:
        try:
            parsed.append(json.loads(obj_text))
        except json.JSONDecodeError:
            continue  # skip the one truncated trailing object, if any

    if not parsed:
        raise ValueError("model output contained no complete task objects (fully truncated)")
    return parsed


@router.post("/{pipeline_id}/epics/suggest", response_model=list[schemas.EpicOut])
def suggest_epics(pipeline_id: int, payload: schemas.SuggestEpicsIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Fallback for when regex-based task extraction finds nothing —
    happens whenever the LLM wrote the Dev Story in a format we didn't
    anticipate (and it will keep happening; LLM output format isn't
    stable). Ask an LLM to read the document and extract real tasks
    instead of guessing at one more regex pattern."""
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    artifact = db.query(models.Artifact).filter(
        models.Artifact.id == payload.artifact_id,
        models.Artifact.pipeline_id == pipeline_id,
    ).first()
    if not artifact:
        raise HTTPException(404, "Artifact not found in this pipeline")

    logger.info(
        "\n%s\n[SUGGEST EPICS] pipeline=%s artifact=%s\n%s",
        "=" * 90, pipeline_id, artifact.id, "-" * 90,
    )
    try:
        # Sprint plans / dev stories can list a lot of tasks — give this
        # call real headroom (well above the router's 4096 default) so
        # the JSON array has room to actually close. _parse_json_array
        # above is still tolerant if a huge document blows even this.
        raw = llm_router.run(TASK_EXTRACTION_PERSONA, artifact.content_md, max_tokens=8192)
        logger.info("[SUGGEST EPICS] raw response (first 500 chars):\n%s\n%s", raw[:500], "=" * 90)
        parsed = _parse_json_array(raw)
    except Exception as e:
        logger.info("[SUGGEST EPICS ERROR] %s\n%s", e, "=" * 90)
        raise HTTPException(500, f"Task extraction failed: {e}")

    existing_count = db.query(models.Epic).filter(models.Epic.pipeline_id == pipeline_id).count()
    created = []
    for i, item in enumerate(parsed):
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        if not title:
            continue
        epic = models.Epic(
            pipeline_id=pipeline.id,
            source_artifact_id=artifact.id,
            title=title,
            content_md=content or title,
            order_index=existing_count + i,
        )
        db.add(epic)
        created.append(epic)
    db.commit()
    for epic in created:
        db.refresh(epic)
    return created


@router.post("/{pipeline_id}/epics", response_model=list[schemas.EpicOut])
def create_epics(pipeline_id: int, payload: schemas.EpicsCreateIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    existing_count = db.query(models.Epic).filter(models.Epic.pipeline_id == pipeline_id).count()
    created = []
    for i, e in enumerate(payload.epics):
        epic = models.Epic(
            pipeline_id=pipeline.id,
            source_artifact_id=e.source_artifact_id,
            title=e.title,
            content_md=e.content_md,
            order_index=existing_count + i,
        )
        db.add(epic)
        created.append(epic)
    db.commit()
    for epic in created:
        db.refresh(epic)
    return created


@router.get("/{pipeline_id}/epics", response_model=list[schemas.EpicOut])
def list_epics(pipeline_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    _get_pipeline_or_404(db, pipeline_id, current_user)
    return db.query(models.Epic).filter(
        models.Epic.pipeline_id == pipeline_id
    ).order_by(models.Epic.order_index.asc()).all()


# ---------- config (YAML view, synced with the canvas) ----------

@router.get("/{pipeline_id}/config", response_model=schemas.ConfigOut)
def get_config(pipeline_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    config = {
        "pipeline": {
            "id": pipeline.id,
            "name": pipeline.name,
            "nodes": [
                {
                    "order": n.order_index,
                    "agent_id": n.agent_id,
                    "status": n.status.value if hasattr(n.status, "value") else n.status,
                }
                for n in sorted(pipeline.nodes, key=lambda x: x.order_index)
            ],
        }
    }
    return schemas.ConfigOut(yaml=yaml.dump(config, sort_keys=False))


@router.put("/{pipeline_id}/config", response_model=schemas.PipelineOut)
def put_config(pipeline_id: int, payload: schemas.ConfigOut, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Rebuild the node ordering from an edited YAML config. Only reorders
    among agent_ids already known; use /nodes to add brand-new nodes first."""
    pipeline = _get_pipeline_or_404(db, pipeline_id, current_user)
    try:
        parsed = yaml.safe_load(payload.yaml)
        node_specs = parsed["pipeline"]["nodes"]
    except Exception as e:
        raise HTTPException(400, f"Invalid config YAML: {e}")

    by_agent = {n.agent_id: n for n in pipeline.nodes}
    for spec in node_specs:
        node = by_agent.get(spec["agent_id"])
        if node:
            node.order_index = spec["order"]
    db.commit()
    _renumber(pipeline, db)
    db.refresh(pipeline)
    return pipeline
