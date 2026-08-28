import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import Base, engine, SessionLocal
from app.routers import agents, pipelines, brd, execution, profiles, workspace, knowledge, auth, admin
from app.bmad_loader import sync_agents_to_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

Base.metadata.create_all(bind=engine)


def _migrate_sqlite():
    """create_all() only creates brand-new tables — it won't add columns
    to a table that already exists from a previous run. Epic gained
    status/status_message/updated_at columns; patch existing DB files
    in place instead of requiring people to delete bmad_studio.db."""
    import sqlite3
    conn = sqlite3.connect("bmad_studio.db")
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(epics)").fetchall()}
        if "status" not in cols:
            conn.execute("ALTER TABLE epics ADD COLUMN status VARCHAR NOT NULL DEFAULT 'pending'")
        if "status_message" not in cols:
            conn.execute("ALTER TABLE epics ADD COLUMN status_message TEXT")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE epics ADD COLUMN updated_at DATETIME")

        agent_cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
        if "identity" not in agent_cols:
            conn.execute("ALTER TABLE agents ADD COLUMN identity TEXT")
        if "menu_items_json" not in agent_cols:
            conn.execute("ALTER TABLE agents ADD COLUMN menu_items_json TEXT")
        if "suitable_roles_json" not in agent_cols:
            conn.execute("ALTER TABLE agents ADD COLUMN suitable_roles_json TEXT")

        node_cols = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_nodes)").fetchall()}
        if "position_x" not in node_cols:
            conn.execute("ALTER TABLE pipeline_nodes ADD COLUMN position_x REAL")
        if "position_y" not in node_cols:
            conn.execute("ALTER TABLE pipeline_nodes ADD COLUMN position_y REAL")
        if "selected_menu_item" not in node_cols:
            conn.execute("ALTER TABLE pipeline_nodes ADD COLUMN selected_menu_item TEXT")
        if "max_tokens" not in node_cols:
            conn.execute("ALTER TABLE pipeline_nodes ADD COLUMN max_tokens INTEGER")

        # owner_id landed with real login/accounts — existing pipelines
        # from before that point are "legacy/unowned" and stay reachable
        # only by an admin (see _get_pipeline_or_404).
        pipeline_cols = {row[1] for row in conn.execute("PRAGMA table_info(pipelines)").fetchall()}
        if "owner_id" not in pipeline_cols:
            conn.execute("ALTER TABLE pipelines ADD COLUMN owner_id INTEGER")

        conn.commit()
    finally:
        conn.close()


_migrate_sqlite()

app = FastAPI(
    title="BMad Studio Backend",
    description="Agent canvas + pipeline orchestration API over the BMad Method framework",
    version="0.1.0",
)

# Wide-open CORS for local dev / Lovable preview. Tighten before any real deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(agents.router)
app.include_router(pipelines.router)
app.include_router(brd.router)
app.include_router(execution.router)
app.include_router(profiles.router)
app.include_router(workspace.router)
app.include_router(knowledge.router)


@app.on_event("startup")
def startup_sync_agents():
    """Scan _bmad/ and populate the agents table on boot, so the canvas
    has a full agent list available immediately."""
    db = SessionLocal()
    try:
        discovered = sync_agents_to_db(db)
        print(f"[startup] synced {len(discovered)} agents from _bmad/")
    finally:
        db.close()


@app.get("/")
def root():
    return {
        "service": "BMad Studio Backend",
        "docs": "/docs",
        "endpoints": [
            "/auth/register", "/auth/login", "/agents", "/pipelines",
            "/brd/upload", "/kb/documents", "/admin/users", "/admin/sessions",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
