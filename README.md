# BMad Studio Backend

FastAPI backend for the BMad agent canvas: upload a BRD, arrange BMad
agents into a linear pipeline, run them one at a time (manual trigger),
edit personas live, and get presentable artifacts out. Built for the
"Visual node/flowchart drag-and-drop" canvas, v1 = linear-only pipelines,
manual Run per node, stale-cascade on downstream nodes.

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

The `_bmad/` folder (your BMad Method framework files) should sit right
next to `app/` — it's already included in this zip. If you swap in a
different `_bmad/` folder later, just replace the folder and hit
`POST /agents/sync` (or restart the server — it syncs on startup).

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

Then open http://127.0.0.1:8000/docs for interactive API docs (Swagger UI).

On startup it scans `_bmad/*/agents/*.md`, parses each agent's persona,
and populates a local `bmad_studio.db` (SQLite, created automatically —
nothing to configure).

## Wiring an LLM (optional — runs with a stub by default)

By default, running a node returns a stub response so you can test the
whole flow with zero API keys. To get real output:

```bash
export LLM_PROVIDER=claude
export ANTHROPIC_API_KEY=sk-ant-...
# optional: export ANTHROPIC_MODEL=claude-sonnet-4-6
```

DeepSeek and Ollama are also wired in `app/llm_router.py` following the
same pattern (`LLM_PROVIDER=deepseek` + `DEEPSEEK_API_KEY`, or
`LLM_PROVIDER=ollama` + local Ollama running).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/agents` | List all discovered BMad agents |
| POST | `/agents/sync` | Re-scan `_bmad/` folder |
| POST | `/brd/upload` | Upload a BRD (file or text) → creates an empty pipeline |
| POST | `/pipelines` | Create a pipeline with an ordered list of agent_ids |
| GET | `/pipelines/{id}` | Get pipeline + all nodes + statuses |
| POST | `/pipelines/{id}/nodes` | Add a node (agent) to the pipeline |
| DELETE | `/pipelines/{id}/nodes/{node_id}` | Remove a node |
| PUT | `/pipelines/{id}/nodes/reorder` | Move a node to a new position |
| PUT | `/pipelines/{id}/nodes/{node_id}/persona` | Live-edit a node's persona |
| POST | `/pipelines/{id}/nodes/{node_id}/run` | Run one node (manual trigger) — marks downstream nodes stale |
| GET | `/pipelines/{id}/artifacts` | List all artifacts (outputs) produced |
| GET/PUT | `/pipelines/{id}/config` | YAML view of the pipeline, editable |

## How a node run works

1. Node's input = BRD text (if it's the first node) + previous node's
   output (if any).
2. Sent to the LLM with the node's `persona_md` as the system prompt.
3. Output saved on the node (`status` → `fresh`) and as a new `Artifact`.
4. Every node **after** this one in the pipeline gets flagged `stale`
   (per the design: re-running upstream invalidates downstream output
   until it's manually re-run too).

## Execution stage (Aider + BMad-agent-as-human + Stakpak)

`WS /ws/execution/{pipeline_id}?epics=<artifact_id1>,<artifact_id2>` —
matches the frontend's `subscribeExecution()` contract exactly. For each
epic (an artifact id from `/pipelines/{id}/artifacts`), in sequence:

1. **Aider** runs as a real subprocess (`aider --message ...`, one-shot
   mode) against a real per-pipeline git workspace at
   `backend/workspaces/pipeline_{id}/`, using the artifact's content as
   its instructions. Requires the `aider-chat` pip package (in
   requirements.txt) and the same `LLM_PROVIDER`/API key env vars used
   for pipeline runs — nothing extra to configure.
2. If Aider's reply ends in a question, it's routed to **BMad-agent-as-
   human** (`app/bmad_human.py`), which answers using the epic's own
   artifact content as its only context, and the answer is fed back to
   Aider to continue (up to 3 rounds).
3. Real file changes are read via `git diff` and streamed as diffs.
4. **Stakpak** stage is a structured **stub** (`app/stakpak_stage.py`) —
   Stakpak is a separate CLI/agent product that needs its own install +
   auth on whatever machine runs this backend, so it can't be wired
   blind. The stub always reports `status: "pending"`, never a fake
   "pass" — swap in real `stakpak` subprocess calls there when ready.

If `LLM_PROVIDER` isn't set, the socket reports a clear error instead of
hanging or crashing — same "fails honest" pattern as the stub LLM
elsewhere in this backend.



## Knowledge Base (admin KB + per-node attachments)

Two independent env switches control the RAG layer (same pattern as
`LLM_PROVIDER` above):

```bash
# where vectors are stored
export KB_VECTOR_STORE=chroma        # or: qdrant
export QDRANT_URL=http://localhost:6333
export QDRANT_COLLECTION=kb_chunks

# who computes the embeddings
export KB_EMBEDDING_PROVIDER=local   # or: ollama
export OLLAMA_HOST=http://localhost:11434
export OLLAMA_EMBED_MODEL=nomic-embed-text
```

Defaults (`chroma` + `local`) need zero config — Chroma stores vectors
in `./chroma_db` and embeds with a bundled local model (one-time ~80MB
download on first use, fully offline after that).

- `POST /kb/documents` — admin uploads a global doc (`.md`/`.txt`/`.pdf`)
  tagged `regulatory_compliance` | `dev_testing_guidelines` |
  `data_glossary` | `custom`, scoped to `applicable_roles` (`pm` and/or
  `developer`). Retrieved automatically for every node run whose
  agent's role matches.
- `GET/DELETE /kb/documents` — list/remove global docs.
- `POST/GET/DELETE /pipelines/{id}/nodes/{node_id}/documents` — attach
  a doc to one specific node only; retrieved only when that node runs.

Retrieved chunks are prepended to a node's input as a "Reference
Context" section before the LLM call — visible in `last_input_text` on
the node for full transparency into what a run actually saw.

## Per-node output token limit

`PUT /pipelines/{id}/nodes/{node_id}/max-tokens` — sets a hard ceiling
on that node's LLM output (`{"max_tokens": 2000}`, or `null` to reset
to the router default of 4096). Applies across Claude, DeepSeek, and
Ollama. Note this is a ceiling only — there's no API-level "minimum
length"; enforce a floor via persona wording if needed.

## Next steps

- Frontend calls these endpoints directly — CORS is wide open (`*`) for
  local dev / Lovable preview; tighten `allow_origins` before any real
  deployment.
- Branching pipelines (parallel agent paths) are a deliberate v2 — v1
  is linear-only by design.
- Wire a real Stakpak CLI call into `app/stakpak_stage.py` once it's
  installed/authenticated on the host machine.
- The "is this a question" heuristic in `aider_runner.py` (last line
  ends in "?") is deliberately simple — worth revisiting once you see
  real Aider output patterns for your codebase.
