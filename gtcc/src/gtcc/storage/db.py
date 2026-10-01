"""Engine and session management."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from gtcc.storage.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def configure(database_url: str, *, echo: bool = False) -> Engine:
    """Create the engine. Called once at startup, and by tests.

    Takes the plain connection string, not the SecretStr that holds it
    in settings: callers pass ``settings.database_dsn`` so that the
    unwrapping is visible at the call site rather than hidden here.
    """
    global _engine, _SessionFactory
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(
        database_url, echo=echo, future=True, connect_args=connect_args, pool_pre_ping=True
    )
    if database_url.startswith("sqlite"):
        # Foreign keys are off by default in SQLite, which would let the
        # development database accept rows PostgreSQL would refuse.
        @event.listens_for(_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover - driver glue
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("database not configured; call gtcc.storage.db.configure() first")
    return _engine


def create_all() -> None:
    """Create tables directly.

    Used by tests and first-run development. Deployments use Alembic so
    that schema changes are reviewable and reversible.
    """
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionFactory is None:
        raise RuntimeError("database not configured; call gtcc.storage.db.configure() first")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
