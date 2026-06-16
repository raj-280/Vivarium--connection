"""
server/db/database.py

SQLAlchemy engine + session factory.
DATABASE_URL comes from config/settings.py — swap to a PostgreSQL URL later
and nothing else needs to change (Section 2.1).

Exposes:
  engine          — the SQLAlchemy engine (for tests or alembic)
  SessionLocal    — sessionmaker bound to the engine
  get_db()        — FastAPI dependency (yields a session, commits/rolls back)
  db_session()    — context-manager session for background tasks
  create_tables() — called once at startup to create all tables
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings
from db.models import Base


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
_connect_args: dict = {}
if settings.DATABASE_URL.startswith("sqlite"):
    # SQLite must allow cross-thread access when used in FastAPI's thread pool.
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    # Set echo=True here temporarily to log every SQL statement while debugging.
    echo=False,
    # Pool settings — SQLite uses StaticPool; PostgreSQL will use QueuePool by default.
    # No pool_size override needed for local SQLite.
)


# ---------------------------------------------------------------------------
# SQLite: enforce foreign key constraints (OFF by default in SQLite).
# This listener is a no-op for PostgreSQL, which enforces FKs natively.
# ---------------------------------------------------------------------------
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
    if settings.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL mode: readers don't block writers and writers don't block readers.
        # This is the correct fix for "database is locked" when multiple sessions
        # coexist in the same process (e.g. cache.set_pending_command opens its
        # own session while the caller's session is still open for reads).
        cursor.execute("PRAGMA journal_mode=WAL")
        # Retry writes for up to 5 seconds before raising OperationalError.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()



# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # Avoids lazy-load errors after commit in async contexts
)


# ---------------------------------------------------------------------------
# FastAPI dependency
# Usage in a route:
#   from db.database import get_db
#   def my_route(db: Session = Depends(get_db)): ...
# ---------------------------------------------------------------------------
def get_db() -> Generator[Session, None, None]:
    """
    Yields a SQLAlchemy session for use as a FastAPI dependency.
    Commits on clean exit; rolls back on exception; always closes.
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Context-manager session — for background tasks outside the request cycle
# (scan engine, position monitor, pending-command sweep, etc.)
# Usage:
#   from db.database import db_session
#   with db_session() as db:
#       db.query(Rack).filter(...).all()
# ---------------------------------------------------------------------------
@contextmanager
def db_session() -> Generator[Session, None, None]:
    """
    Context-manager session for use outside a FastAPI request context.
    Commits on clean exit; rolls back on exception; always closes.
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Table creation — called once in main.py lifespan at server startup.
# create_all() is idempotent: it skips tables that already exist.
# On PostgreSQL, use Alembic for migrations instead of create_all().
# ---------------------------------------------------------------------------
def create_tables() -> None:
    """Create all ORM-defined tables if they do not already exist."""
    Base.metadata.create_all(bind=engine)
