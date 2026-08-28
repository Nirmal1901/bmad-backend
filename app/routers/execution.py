"""
WebSocket endpoint powering Screen 4 (Execution). Matches the frontend's
existing `subscribeExecution(epicIds, onUpdate)` contract in
executionApi.ts exactly — same ExecutionStreams shape — so switching
`USE_MOCKS = false` and pointing this at a WebSocket is the only
frontend change needed.

Flow per epic, run in sequence: Aider (+ agent-as-human answering
any question it asks) -> Stakpak stage (stub, see stakpak_stage.py).
"""
import queue
import threading
import time
import logging
import os
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import models
from app.aider_runner import run_epic
from app.stakpak_stage import run_stakpak_stage
from app.auth import _decode, is_admin

router = APIRouter(tags=["execution"])
logger = logging.getLogger("bmad_studio.execution")


def _authenticate_ws(token: str, pipeline_id: int) -> models.User:
    """WebSockets can't send an Authorization header from the browser,
    so the token comes in as a query param instead — same JWT as
    everywhere else. Raises WebSocketDisconnect-friendly ValueError on
    any auth/ownership failure; caller closes the socket."""
    if not token:
        raise ValueError("missing token")
    payload = _decode(token)
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()
        if not user:
            raise ValueError("unknown user")
        pipeline = db.query(models.Pipeline).filter(models.Pipeline.id == pipeline_id).first()
        if not pipeline:
            raise ValueError("pipeline not found")
        if not is_admin(user) and pipeline.owner_id is not None and pipeline.owner_id != user.id:
            raise ValueError("not your session")
        return user
    finally:
        db.close()


def _epic_context(pipeline_id: int, epic_id: str) -> str:
    """Build the context handed to Aider and to the agent-as-human.

    Correct model: BA report + Architecture doc + Dev Story etc are all
    knowledge base — referenced for EVERY epic, not treated as separate
    epics themselves. A real epic is a row in the `epics` table (a
    single task extracted from a Dev Story's task list, e.g. "Task 3:
    Implement mock auth service") — that's what actually gets
    implemented. Every Artifact in the pipeline still comes along as
    reference context, so Aider and the agent-as-human never lose sight
    of the BRD, architecture decisions, etc. while working a single epic.
    """
    db: Session = SessionLocal()
    try:
        artifacts = db.query(models.Artifact).filter(
            models.Artifact.pipeline_id == pipeline_id
        ).order_by(models.Artifact.created_at.asc()).all()

        epic_row = None
        try:
            epic_row = db.query(models.Epic).filter(
                models.Epic.pipeline_id == pipeline_id,
                models.Epic.id == int(epic_id),
            ).first()
        except (ValueError, TypeError):
            pass  # epic_id wasn't a valid int -> fall through to legacy artifact matching

        if not artifacts and not epic_row:
            return f"(No artifacts or epics found for pipeline {pipeline_id} — epic: {epic_id})"

        sections = []

        if epic_row:
            sections.append(
                f"## [PRIMARY EPIC — IMPLEMENT THIS] {epic_row.title}\n\n{epic_row.content_md}"
            )
            for a in artifacts:
                title = a.title or f"artifact {a.id}"
                sections.append(
                    f"## [KNOWLEDGE BASE (reference only, do not re-implement)] {title}\n\n{a.content_md}"
                )
            return "\n\n---\n\n".join(sections)

        # Legacy fallback: no Epic row matched (e.g. an old link, or the
        # caller passed a raw artifact id) — treat the matched artifact
        # itself as primary, same behavior as before Epics existed.
        primary = next(
            (a for a in artifacts
             if str(a.id) == str(epic_id) or str(a.node_id) == str(epic_id)),
            None
        )
        for a in artifacts:
            label = "PRIMARY EPIC — IMPLEMENT THIS" if a is primary else "KNOWLEDGE BASE (reference only, do not re-implement)"
            title = a.title or f"artifact {a.id}"
            sections.append(f"## [{label}] {title}\n\n{a.content_md}")
        if primary is None:
            sections.insert(0,
                "NOTE: no epic or artifact matched this id — treat all "
                "of the below as knowledge base and use your judgement on "
                "what remains to be implemented.")
        return "\n\n---\n\n".join(sections)
    finally:
        db.close()


def _set_epic_status(pipeline_id: int, epic_id: str, status: "models.EpicStatus",
                      message: str | None = None) -> None:
    """Persist the epic's execution status to the DB — this is what
    survives a page reload/revisit, not just the in-memory websocket
    state. epic_id may be a legacy non-numeric id (e.g. "epic-1"); those
    just have nothing to persist against."""
    try:
        epic_pk = int(epic_id)
    except (ValueError, TypeError):
        return
    db: Session = SessionLocal()
    try:
        epic = db.query(models.Epic).filter(
            models.Epic.pipeline_id == pipeline_id,
            models.Epic.id == epic_pk,
        ).first()
        if epic:
            epic.status = status
            if message is not None:
                epic.status_message = message
            db.commit()
    finally:
        db.close()


def _worker(pipeline_id: int, epic_ids: list[str], q: "queue.Queue"):
    stakpak_enabled = os.environ.get("ENABLE_STAKPAK_STAGE", "false").lower() == "true"
    for epic_id in epic_ids:
        _set_epic_status(pipeline_id, epic_id, models.EpicStatus.running)
        q.put({"type": "epic_status", "epicId": epic_id, "status": "running"})
        try:
            context = _epic_context(pipeline_id, epic_id)
            epic_errored = False
            error_message = None
            for event in run_epic(pipeline_id, epic_id, context):
                event["epicId"] = epic_id
                if event.get("type") == "status" and event.get("status") == "error":
                    epic_errored = True
                    error_message = event.get("message")
                q.put(event)
            if epic_errored:
                _set_epic_status(pipeline_id, epic_id, models.EpicStatus.error, error_message)
                q.put({"type": "epic_status", "epicId": epic_id, "status": "error",
                       "message": error_message})
                continue
            if stakpak_enabled:
                stage_errored = False
                for event in run_stakpak_stage(epic_id):
                    event["epicId"] = epic_id
                    if event.get("type") == "stakpak_update" and event.get("status") == "fail":
                        stage_errored = True
                    q.put(event)
                if stage_errored:
                    _set_epic_status(pipeline_id, epic_id, models.EpicStatus.error,
                                      "Stakpak stage failed")
                    q.put({"type": "epic_status", "epicId": epic_id, "status": "error"})
                    continue
            _set_epic_status(pipeline_id, epic_id, models.EpicStatus.done)
            q.put({"type": "epic_status", "epicId": epic_id, "status": "done"})
        except Exception as e:
            # Any unexpected crash (OS/filesystem errors, etc.) still
            # needs to reach the frontend as a clean error, not hang the
            # queue forever with no more events coming.
            logger.exception("Unhandled error running epic %s", epic_id)
            _set_epic_status(pipeline_id, epic_id, models.EpicStatus.error, str(e))
            q.put({
                "type": "status", "status": "error", "epicId": epic_id,
                "message": f"Unexpected error: {e}",
            })
            q.put({"type": "epic_status", "epicId": epic_id, "status": "error",
                   "message": str(e)})
    q.put(None)  # sentinel: all epics done


def _initial_epic_statuses(pipeline_id: int, epic_ids: list[str]) -> dict:
    """Seed epicStatuses from whatever's already persisted (e.g. a
    previous run finished some of these and the page was reloaded),
    defaulting anything unknown/non-numeric to 'pending'."""
    statuses = {eid: "pending" for eid in epic_ids}
    db: Session = SessionLocal()
    try:
        numeric_ids = []
        for eid in epic_ids:
            try:
                numeric_ids.append(int(eid))
            except (ValueError, TypeError):
                pass
        if numeric_ids:
            rows = db.query(models.Epic).filter(
                models.Epic.pipeline_id == pipeline_id,
                models.Epic.id.in_(numeric_ids),
            ).all()
            for row in rows:
                status_val = row.status.value if hasattr(row.status, "value") else row.status
                statuses[str(row.id)] = status_val
    finally:
        db.close()
    return statuses


@router.websocket("/ws/execution/{pipeline_id}")
async def execution_ws(websocket: WebSocket, pipeline_id: int, epics: str = "", token: str = ""):
    try:
        _authenticate_ws(token, pipeline_id)
    except Exception as e:
        await websocket.close(code=4401, reason=f"unauthorized: {e}")
        return

    await websocket.accept()
    epic_ids = [e for e in epics.split(",") if e] or ["epic-1"]

    q: queue.Queue = queue.Queue()
    thread = threading.Thread(target=_worker, args=(pipeline_id, epic_ids, q), daemon=True)
    thread.start()

    state = {
        "aider": [], "agent": [], "stakpak": [], "status": "running",
        "epicStatuses": _initial_epic_statuses(pipeline_id, epic_ids),
    }
    aider_counter = agent_counter = stakpak_counter = 0

    try:
        while True:
            event = await _get_from_queue(q)
            if event is None:
                state["status"] = "done"
                await websocket.send_json(state)
                break

            etype = event.get("type")
            now_ms = int(time.time() * 1000)

            if etype == "aider_diff":
                aider_counter += 1
                state["aider"] = ([{
                    "id": event.get("id", f"a-{aider_counter}"),
                    "epicId": event["epicId"],
                    "file": event["file"],
                    "added": event["added"],
                    "removed": event["removed"],
                    "ts": now_ms,
                }] + state["aider"])[:30]

            elif etype == "agent_message":
                agent_counter += 1
                state["agent"] = (state["agent"] + [{
                    "id": f"b-{agent_counter}",
                    "epicId": event["epicId"],
                    "from": event["from"],
                    "text": event["text"],
                    "ts": now_ms,
                }])[-30:]

            elif etype == "stakpak_update":
                stakpak_counter += 1
                state["stakpak"] = (state["stakpak"] + [{
                    "id": f"s-{stakpak_counter}",
                    "epicId": event["epicId"],
                    "step": event["step"],
                    "status": event["status"],
                    "message": event["message"],
                    "ts": now_ms,
                }])[-20:]

            elif etype == "epic_status":
                state["epicStatuses"] = {
                    **state["epicStatuses"],
                    event["epicId"]: event["status"],
                }

            elif etype == "status" and event.get("status") == "error":
                state["status"] = "error"
                state["agent"] = (state["agent"] + [{
                    "id": f"b-err-{agent_counter+1}",
                    "epicId": event["epicId"],
                    "from": "agent",
                    "text": f"ERROR: {event.get('message', 'unknown error')}",
                    "ts": now_ms,
                }])[-30:]

            await websocket.send_json(state)

    except WebSocketDisconnect:
        pass


async def _get_from_queue(q: "queue.Queue"):
    """Bridge the blocking worker-thread queue into the async websocket
    loop without busy-polling."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, q.get)