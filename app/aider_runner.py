"""
Runs Aider against a real per-pipeline git workspace, one epic at a
time, as a one-shot subprocess call (Aider's --message mode: no
terminal interaction). Detects when Aider's response is effectively a
question, routes it to the agent's "AI human" for an answer, and
continues the same chat via --restore-chat-history. After the round(s)
complete, reads the actual `git diff` to report real file changes.

This is real subprocess integration, not a mock — it needs `aider`
installed (pip install aider-chat, already in requirements.txt) and a
working LLM_PROVIDER + API key (same env vars as pipeline runs).
"""
import os
import re
import subprocess
import logging
import threading
from pathlib import Path
from typing import Iterator

from app.bmad_human import answer_question

logger = logging.getLogger("bmad_studio.aider")

WORKSPACES_ROOT = Path(__file__).resolve().parent.parent / "workspaces"
MAX_QA_ROUNDS = 3
AIDER_TIMEOUT_SECONDS = 30000

# React StrictMode (and plain double-clicks) can open two WS connections
# for the same pipeline almost simultaneously, spawning two worker
# threads that both try to `git init` the same folder at once — that
# race corrupts the partial .git dir. Serialize workspace creation per
# pipeline so the second caller just waits and reuses what the first
# one created, instead of racing it.
_workspace_locks: dict[int, threading.Lock] = {}
_workspace_locks_guard = threading.Lock()


def _get_workspace_lock(pipeline_id: int) -> threading.Lock:
    with _workspace_locks_guard:
        if pipeline_id not in _workspace_locks:
            _workspace_locks[pipeline_id] = threading.Lock()
        return _workspace_locks[pipeline_id]


def _aider_model_flag() -> list[str]:
    """Map our existing LLM_PROVIDER env vars to an Aider/litellm model
    string, so the same keys that power pipeline runs power Aider too."""
    provider = os.environ.get("LLM_PROVIDER", "stub")
    if provider == "claude":
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        return ["--model", f"anthropic/{model}"]
    if provider == "deepseek":
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        return ["--model", f"deepseek/{model}"]
    if provider == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "llama3")
        return ["--model", f"ollama/{model}"]
    return []  # stub/unset: let Aider fail fast with a clear "no model" message


def _ensure_workspace(pipeline_id: int) -> Path:
    lock = _get_workspace_lock(pipeline_id)
    with lock:
        ws = WORKSPACES_ROOT / f"pipeline_{pipeline_id}"
        ws.mkdir(parents=True, exist_ok=True)
        git_dir = ws / ".git"
        if not (git_dir / "HEAD").exists():
            # .git may exist but be incomplete (a prior crashed/raced
            # attempt) — clear it out before re-initializing rather than
            # letting `git init` collide with a half-built directory.
            if git_dir.exists():
                import shutil
                shutil.rmtree(git_dir, ignore_errors=True)
            subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
            subprocess.run(["git", "config", "user.email", "bmad-studio@local"], cwd=ws, check=True)
            subprocess.run(["git", "config", "user.name", "BMad Studio"], cwd=ws, check=True)
            (ws / ".gitignore").write_text(".aider*\n.bmad/\n")
            subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init workspace"], cwd=ws, check=True)
        return ws


def _run_aider_once(workspace: Path, message: str, continue_chat: bool, read_files: list[str]) -> str:
    cmd = [
        "aider", "--yes-always", "--no-auto-commits", "--no-analytics",
        "--no-check-update",
        # Separate from --no-check-update: aider has its own "would you
        # like to see what's new?" prompt that fires on the FIRST run of
        # any given aider version on a machine (tracked in its own local
        # cache, unrelated to anything in this app). With --yes-always
        # and no real tty, that prompt auto-confirms and calls
        # webbrowser.open() on release notes — which, since this backend
        # runs locally, hijacks whatever browser tab you actually had
        # open. --no-show-release-notes disables that path entirely,
        # regardless of first-run state.
        "--no-show-release-notes",
        *(["--restore-chat-history"] if continue_chat else []),
        *_aider_model_flag(),
        *[flag for f in read_files for flag in ("--read", f)],
        "--message", message,
    ]
    result = subprocess.run(
        cmd, cwd=workspace, capture_output=True, text=True,
        timeout=AIDER_TIMEOUT_SECONDS,
        env={**os.environ, "BROWSER": "true"},  # belt-and-suspenders: even
        # if some other aider/litellm/dependency code path ever calls
        # webbrowser.open() for any other reason, this makes Python's
        # webbrowser module invoke the no-op `true` command instead of a
        # real browser, so nothing this subprocess does can ever hijack
        # the user's browser.
    )
    return (result.stdout or "") + ("\n" + result.stderr if result.returncode != 0 else "")


def _looks_like_question(aider_output: str) -> str | None:
    """Very deliberately simple heuristic: last non-empty line, if it
    ends in '?', is treated as Aider asking something. Good enough for
    v1 — Aider's one-shot chat replies are usually short."""
    lines = [l.strip() for l in aider_output.splitlines() if l.strip()]
    if not lines:
        return None
    last = lines[-1]
    if last.endswith("?") and len(last) < 400:
        return last
    return None


def _git_diff_structured(workspace: Path, epic_id: str) -> list[dict]:
    """Parse `git diff` (unstaged, since we run with --no-auto-commits)
    into the AiderDiff shape the frontend expects: one entry per file
    with added/removed line lists."""
    result = subprocess.run(
        ["git", "diff", "--unified=0"], cwd=workspace,
        capture_output=True, text=True,
    )
    diffs = []
    current_file = None
    added, removed = [], []

    def flush():
        if current_file and (added or removed):
            diffs.append({
                "id": f"a-{epic_id}-{len(diffs)}",
                "epicId": epic_id,
                "file": current_file,
                "added": added[:],
                "removed": removed[:],
            })

    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            flush()
            current_file = line[6:]
            added, removed = [], []
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line)
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line)
    flush()
    return diffs


def run_epic(pipeline_id: int, epic_id: str, epic_context: str) -> Iterator[dict]:
    """Generator yielding events as dicts:
      {"type": "agent_message", "from": "aider"|"agent", "text": ...}
      {"type": "aider_diff", "file": ..., "added": [...], "removed": [...]}
      {"type": "status", "status": "running"|"done"|"error", "message": ...}
    Caller (the WebSocket route) streams these out as they arrive.
    """
    workspace = _ensure_workspace(pipeline_id)

    if os.environ.get("LLM_PROVIDER", "stub") == "stub":
        yield {"type": "status", "status": "error",
               "message": "No LLM_PROVIDER configured — set LLM_PROVIDER + API key "
                          "(same env vars used for pipeline runs) before running Aider."}
        return

    # Write the KB as a real file and attach it read-only (same idea as
    # @file in Cursor / --read in Aider) instead of pasting the whole
    # thing into the chat message every round — cleaner logs, no
    # arbitrary truncation, and Aider treats it as proper file context.
    context_dir = workspace / ".bmad"
    context_dir.mkdir(exist_ok=True)
    context_filename = f"context_{epic_id}.md"
    (context_dir / context_filename).write_text(epic_context)
    read_files = [f".bmad/{context_filename}"]

    initial_message = (
        f"Read the attached file .bmad/{context_filename} — it's your knowledge "
        f"base for this project. One section is marked PRIMARY EPIC — IMPLEMENT "
        f"THIS; that is your actual task. Every other section is reference "
        f"context (BRD, prior analysis, architecture decisions) — use it to make "
        f"consistent decisions, but do not re-implement it. Make the minimal, "
        f"correct code changes needed for the primary epic only. If you truly "
        f"need a product decision to proceed, end your reply with a single "
        f"direct question."
    )

    message = initial_message
    continue_chat = False

    for round_n in range(MAX_QA_ROUNDS + 1):
        yield {"type": "status", "status": "running",
               "message": f"Aider round {round_n + 1} for {epic_id}"}
        logger.info(
            "\n%s\n[AIDER] epic=%s round=%s\nCONTEXT FILE: .bmad/%s (%d chars)\nMESSAGE SENT:\n%s\n%s",
            "=" * 90, epic_id, round_n + 1, context_filename, len(epic_context), message, "-" * 90,
        )
        try:
            output = _run_aider_once(workspace, message, continue_chat, read_files)
        except subprocess.TimeoutExpired:
            yield {"type": "status", "status": "error", "message": "Aider timed out"}
            return
        except FileNotFoundError:
            yield {"type": "status", "status": "error",
                   "message": "`aider` is not installed. pip install aider-chat"}
            return

        logger.info("[AIDER] raw output (first 500 chars):\n%s\n%s", output[:500], "=" * 90)

        question = _looks_like_question(output)
        if question:
            logger.info("[AIDER->AGENT] question: %s", question)
            yield {"type": "agent_message", "from": "aider", "text": question}
            answer = answer_question(epic_context, question)
            logger.info("[AGENT->AIDER] answer: %s", answer)
            yield {"type": "agent_message", "from": "agent", "text": answer}
            message = answer
            continue_chat = True
            continue
        break  # no question -> Aider considers the epic done for this round

    for d in _git_diff_structured(workspace, epic_id):
        yield {"type": "aider_diff", **d}

    yield {"type": "status", "status": "done", "message": f"{epic_id} complete"}