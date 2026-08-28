"""
Read-only browsing of a pipeline's real Aider workspace
(backend/workspaces/pipeline_{id}/) — the actual git repo Aider writes
code into. Powers the IDE-style file tree + file viewer on the
execution screen. Also exposes a small console endpoint for running
shell commands scoped to that same workspace directory — the hook
point for wiring in NuOps later; for now it just runs the command and
hands back stdout/stderr.
"""
import shlex
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy.orm import Session

from app.aider_runner import WORKSPACES_ROOT
from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, is_admin

router = APIRouter(prefix="/pipelines", tags=["workspace"])

# Commands that are flat-out refused regardless of workspace scoping —
# this console runs with the backend process's own permissions, so
# nothing that reaches outside the sandboxed workspace or touches the
# host/network is allowed. Extend cautiously.
BLOCKED_COMMAND_PREFIXES = (
    "rm -rf /", "sudo", "shutdown", "reboot", "mkfs", ":(){", "curl", "wget",
)
CONSOLE_TIMEOUT_SECONDS = 30


def _require_session_access(pipeline_id: int, db: Session, current_user: models.User):
    pipeline = db.query(models.Pipeline).filter(models.Pipeline.id == pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, f"Pipeline {pipeline_id} not found")
    if not is_admin(current_user) and pipeline.owner_id is not None and pipeline.owner_id != current_user.id:
        raise HTTPException(403, "You don't have access to this session")
    return pipeline

# Directories nobody needs to see in a code browser — VCS internals and
# our own scratch context files, not part of "the generated code".
IGNORED_DIRS = {".git", ".bmad", "node_modules", "__pycache__", ".aider.chat.history.md"}

# Rough cap so someone can't ask us to stream a multi-hundred-MB binary
# into a JSON response meant for a text viewer.
MAX_FILE_BYTES = 1_500_000

TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".yaml", ".yml",
    ".css", ".scss", ".html", ".xml", ".toml", ".ini", ".cfg", ".sh", ".env",
    ".gitignore", ".sql", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".vue", ".svelte", ".graphql", ".proto", ".lock",
}


def _workspace_dir(pipeline_id: int) -> Path:
    ws = WORKSPACES_ROOT / f"pipeline_{pipeline_id}"
    return ws


def _safe_resolve(workspace: Path, rel_path: str) -> Path:
    """Resolve rel_path under workspace, refusing anything that
    escapes it (../../etc, absolute paths, symlink tricks)."""
    candidate = (workspace / rel_path).resolve()
    workspace_resolved = workspace.resolve()
    if candidate != workspace_resolved and workspace_resolved not in candidate.parents:
        raise HTTPException(400, "Invalid path")
    return candidate


def _build_tree(dir_path: Path, workspace_root: Path) -> list[dict]:
    entries = []
    try:
        children = sorted(
            dir_path.iterdir(),
            key=lambda p: (p.is_file(), p.name.lower()),  # dirs first, then alpha
        )
    except FileNotFoundError:
        return []

    for child in children:
        if child.name in IGNORED_DIRS or child.name.startswith("."):
            continue
        rel = str(child.relative_to(workspace_root))
        if child.is_dir():
            node_children = _build_tree(child, workspace_root)
            if not node_children:
                continue  # skip empty dirs — usually just an ignored subtree
            entries.append({
                "name": child.name,
                "path": rel,
                "type": "dir",
                "children": node_children,
            })
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            entries.append({
                "name": child.name,
                "path": rel,
                "type": "file",
                "size": size,
            })
    return entries


@router.get("/{pipeline_id}/workspace/tree")
def get_workspace_tree(
    pipeline_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_session_access(pipeline_id, db, current_user)
    workspace = _workspace_dir(pipeline_id)
    if not workspace.exists():
        return {"exists": False, "tree": []}
    return {"exists": True, "tree": _build_tree(workspace, workspace)}


@router.get("/{pipeline_id}/workspace/file")
def get_workspace_file(
    pipeline_id: int, path: str = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_session_access(pipeline_id, db, current_user)
    workspace = _workspace_dir(pipeline_id)
    if not workspace.exists():
        raise HTTPException(404, "No workspace yet for this pipeline — run an epic first.")
    target = _safe_resolve(workspace, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")

    ext = target.suffix.lower()
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        return {"path": path, "truncated": True, "content": "", "size": size,
                "binary": False, "message": "File too large to preview here."}

    if ext not in TEXT_EXTENSIONS and ext != "":
        # Unknown extension — try to sniff as text, fall back to a
        # "binary, can't preview" response rather than dumping raw bytes.
        try:
            content = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            return {"path": path, "truncated": False, "content": "", "size": size,
                    "binary": True, "message": "Binary file — preview not available."}
    else:
        try:
            content = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            return {"path": path, "truncated": False, "content": "", "size": size,
                    "binary": True, "message": "Binary file — preview not available."}

    return {"path": path, "truncated": False, "content": content, "size": size, "binary": False}


@router.post("/{pipeline_id}/workspace/console", response_model=schemas.ConsoleCommandOut)
def run_console_command(
    pipeline_id: int,
    payload: schemas.ConsoleCommandIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Console panel under the IDE view. Runs a shell command with its
    cwd pinned to this pipeline's own workspace directory — can't be
    pointed anywhere else. This is intentionally the simplest possible
    thing (run command, return output) so it's a clean hook point to
    wire NuOps into later; it isn't a general-purpose remote shell.
    """
    pipeline = _require_session_access(pipeline_id, db, current_user)
    workspace = _workspace_dir(pipeline_id)
    if not workspace.exists():
        raise HTTPException(404, "No workspace yet for this session — run an epic first.")

    command = payload.command.strip()
    if not command:
        raise HTTPException(400, "command is required")

    lowered = command.lower()
    if any(lowered.startswith(bad) for bad in BLOCKED_COMMAND_PREFIXES):
        raise HTTPException(400, "That command isn't allowed from the console.")

    try:
        args = shlex.split(command)
    except ValueError as e:
        raise HTTPException(400, f"Couldn't parse command: {e}")
    if not args:
        raise HTTPException(400, "command is required")

    try:
        result = subprocess.run(
            args,
            cwd=str(workspace.resolve()),
            capture_output=True,
            text=True,
            timeout=CONSOLE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return schemas.ConsoleCommandOut(command=command, stdout="", stderr=f"command not found: {args[0]}", exit_code=127)
    except subprocess.TimeoutExpired:
        return schemas.ConsoleCommandOut(command=command, stdout="", stderr=f"timed out after {CONSOLE_TIMEOUT_SECONDS}s", exit_code=124)

    return schemas.ConsoleCommandOut(
        command=command,
        stdout=result.stdout[-20000:],
        stderr=result.stderr[-20000:],
        exit_code=result.returncode,
    )
