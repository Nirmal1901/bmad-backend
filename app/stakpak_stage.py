"""
Stakpak stage: correct/debug/deploy, run after Aider finishes an epic.

This is a STUB, not a real integration — Stakpak is a separate CLI/agent
product (github.com/stakpak/agent) that needs its own install + auth on
whatever machine actually runs it; it isn't something this backend can
wire blind. The interface below is shaped so swapping in a real
`subprocess.run(["stakpak", ...])` call is a small, contained change —
nothing upstream (the WebSocket route, the frontend) needs to change.
"""
import time
from typing import Iterator

STEPS = ["correcting", "debugging", "deploying", "done"]


def run_stakpak_stage(epic_id: str) -> Iterator[dict]:
    """Yields StakpakUpdate-shaped dicts. Replace the body of this loop
    with real `stakpak` CLI calls (e.g. `stakpak run --epic {epic_id}`)
    once it's installed and authenticated on the host running this
    backend. Kept structurally honest: status is always "pending" here,
    never fabricating a "pass", since we haven't actually run anything."""
    for step in STEPS:
        yield {
            "type": "stakpak_update",
            "step": step,
            "status": "pending",
            "message": f"[stub] {step} not yet wired to a real Stakpak run — "
                       f"see app/stakpak_stage.py",
        }
        time.sleep(0.3)
