"""
SQLite database setup for BMad Studio backend.
Simple, file-based, zero-ops. Swap SQLALCHEMY_DATABASE_URL for Postgres later
if this needs to scale past single-user/demo use.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = "sqlite:///./bmad_studio.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _):
    """WAL mode lets readers (the frontend's polling GET /pipelines/{id})
    proceed while a write is in progress instead of hitting 'database is
    locked'; busy_timeout makes SQLite retry for a bit instead of
    failing immediately on the rare remaining contention. Without this,
    a long-running node (run-stream) writing while the UI polls can
    intermittently 500 mid-stream — which shows up in the browser as
    ERR_INCOMPLETE_CHUNKED_ENCODING, not as a clear error."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

