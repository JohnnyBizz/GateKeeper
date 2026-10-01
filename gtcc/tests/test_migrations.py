"""Alembic migrations, exercised rather than assumed.

The rest of the suite builds its schema with ``create_all``, which
reads the ORM metadata directly. That is fast and it proves nothing
about production, where the schema comes from
``alembic upgrade head``. A migration that was never run can be broken
in ways no amount of green unit tests would reveal, and the first
symptom would be a deployment that will not start.

So these tests do what a deployment does: empty database, upgrade to
head, then use the result.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from gtcc.storage.models import (
    AuditLog,
    Base,
    OrderRow,
    RiskStateRow,
    TradeJournalEntry,
    TradingAccount,
    User,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    """Run the real Alembic CLI, the way a deployment does.

    A subprocess rather than Alembic's Python API, so that what is
    tested is the command a deploy script actually runs, including the
    ini file and ``migrations/env.py`` reading the environment.

    The parent environment is inherited and only the database URL
    overridden: building one from scratch drops the interpreter's own
    package paths and the command fails for a reason that has nothing
    to do with the migration.
    """
    environment = dict(os.environ)
    environment["GTCC_DATABASE_URL"] = database_url
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=240,
    )


@pytest.fixture
def migrated(tmp_path) -> str:
    """An empty database brought to head by Alembic itself."""
    database_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    result = _alembic(database_url, "upgrade", "head")
    assert result.returncode == 0, (
        f"alembic upgrade head failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return database_url


@pytest.mark.slow
class TestTheMigrationRuns:
    def test_an_empty_database_reaches_head(self, migrated):
        engine = create_engine(migrated)
        tables = set(inspect(engine).get_table_names())

        assert "alembic_version" in tables
        # Every table the ORM declares must exist after the upgrade.
        missing = set(Base.metadata.tables) - tables
        assert not missing, f"migration did not create: {sorted(missing)}"

    def test_the_migrated_schema_matches_the_orm(self, migrated):
        """Drift check. If a model changed without a migration, the
        deployment and the code disagree and this is where it shows."""
        result = _alembic(migrated, "check")

        assert result.returncode == 0, (
            "alembic check reported drift between the models and the "
            f"migrations:\n{result.stdout}\n{result.stderr}"
        )

    def test_the_migration_reverses(self, migrated):
        """A migration that cannot be undone is a one-way door."""
        result = _alembic(migrated, "downgrade", "base")

        assert result.returncode == 0, result.stderr
        remaining = set(inspect(create_engine(migrated)).get_table_names())
        assert remaining <= {"alembic_version"}

    def test_the_head_revision_is_recorded(self, migrated):
        engine = create_engine(migrated)
        with engine.connect() as connection:
            from sqlalchemy import text

            stamped = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        history = _alembic(migrated, "heads")
        assert stamped in history.stdout, (
            f"database is stamped {stamped}, which is not a head: {history.stdout}"
        )


@pytest.mark.slow
class TestTheApplicationWorksOnTheMigratedSchema:
    """Creating tables is not the same as being able to use them.

    Column types, constraints and defaults all differ between what
    ``create_all`` produces and what a hand-reviewed migration does, so
    the flows are exercised against the migrated database.
    """

    def test_a_full_persistence_flow_round_trips(self, migrated):
        engine = create_engine(migrated)
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

        with Session(engine) as session:
            account = TradingAccount(
                external_id="MIG-1", broker="paper", mode="PAPER",
                currency="USD", starting_equity=Decimal("100000.1234567890"),
            )
            session.add(account)
            session.flush()

            session.add(
                User(email="m@example.com", password_hash="$argon2id$fake", role="owner")
            )
            session.add(
                OrderRow(
                    client_order_id="ord-1", account_id=account.id, symbol="BTCUSDT",
                    market="CRYPTO", side="BUY", order_type="MARKET", status="FILLED",
                    mode="PAPER", strategy="breakout", quantity=Decimal("0.5"),
                    filled_quantity=Decimal("0.5"), average_fill_price=Decimal("60000.01"),
                    fees_paid=Decimal("1.5"), risk_verdict={"action": "ALLOW", "checks": 33},
                )
            )
            session.add(
                TradeJournalEntry(
                    trade_id="trd-1", account_id=account.id, considered_at=now,
                    symbol="BTCUSDT", market="CRYPTO", strategy="breakout",
                    direction="LONG", mode="PAPER", outcome="REJECTED_BY_RISK",
                    planned_entry=Decimal("60000"), planned_stop=Decimal("59000"),
                    planned_targets=[62500], risk_verdict={"reasons": ["STOP_PRESENT"]},
                )
            )
            session.add(
                RiskStateRow(
                    account_id=account.id, day_start_equity=Decimal("100000"),
                    week_start_equity=Decimal("100000"), peak_equity=Decimal("100000"),
                    current_day="2026-10-01", week_start="2026-09-28",
                    daily_breaker_tripped=True, disabled_symbols=["BTCUSDT"],
                )
            )
            session.add(AuditLog(action="control.live_armed", detail={"armed_by": "m"}))
            session.commit()

        with Session(engine) as session:
            order = session.scalar(select(OrderRow).where(OrderRow.client_order_id == "ord-1"))
            assert order.average_fill_price == Decimal("60000.01")
            assert order.risk_verdict["checks"] == 33

            # Money precision must survive the migrated column type.
            stored = session.scalar(select(TradingAccount)).starting_equity
            assert stored == Decimal("100000.1234567890")

            # A rejected setup is a journal row, which is the point of
            # keeping them.
            rejected = session.scalar(select(TradeJournalEntry))
            assert rejected.outcome == "REJECTED_BY_RISK"
            assert rejected.planned_targets == [62500]

            risk = session.scalar(select(RiskStateRow))
            assert risk.daily_breaker_tripped is True
            assert risk.disabled_symbols == ["BTCUSDT"]

            audit = session.scalar(select(AuditLog))
            assert audit.detail["armed_by"] == "m"
            assert audit.occurred_at.tzinfo is not None, "timestamps must come back aware"

    def test_the_unique_constraints_are_present(self, migrated):
        """create_all and a migration can disagree about constraints,
        and a missing one is silent until duplicate data arrives."""
        from sqlalchemy.exc import IntegrityError

        engine = create_engine(migrated)
        with Session(engine) as session:
            session.add(User(email="dup@example.com", password_hash="x", role="owner"))
            session.commit()

        with Session(engine) as session:
            session.add(User(email="dup@example.com", password_hash="y", role="owner"))
            with pytest.raises(IntegrityError):
                session.commit()

    def test_the_application_starts_against_the_migrated_database(self, migrated, limits,
                                                                  paper_broker, data_adapter, now):
        """The deployment path end to end: migrate, then boot."""
        import secrets

        from fastapi.testclient import TestClient

        from gtcc.adapters.base import AdapterRegistry
        from gtcc.api.app import create_app
        from gtcc.api.security import hash_password
        from gtcc.config import Settings, set_settings
        from gtcc.runtime import TradingRuntime
        from gtcc.storage import db

        set_settings(None)
        settings = Settings(
            secret_key=secrets.token_urlsafe(48), database_url=migrated,
            environment="development", secure_cookies=False, log_level="WARNING",
        )
        set_settings(settings)
        try:
            db.configure(migrated)
            with db.session_scope() as session:
                session.add(
                    User(
                        email="boot@example.com",
                        password_hash=hash_password("a-long-enough-password"),
                        role="owner",
                    )
                )

            registry = AdapterRegistry()
            registry.register_data(data_adapter)
            registry.register_broker(paper_broker)
            runtime = TradingRuntime(
                settings=settings, limits=limits, registry=registry,
                broker_name="paper", data_name=data_adapter.name, clock=lambda: now,
            )
            client = TestClient(create_app(settings=settings, runtime=runtime))

            assert client.get("/api/health").status_code == 200

            login = client.post(
                "/api/auth/login",
                json={"email": "boot@example.com", "password": "a-long-enough-password"},
            )
            assert login.status_code == 200
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]

            # An order writes an audit row through the migrated schema.
            placed = client.post(
                "/api/orders",
                json={
                    "symbol": "BTCUSDT", "market": "CRYPTO", "side": "BUY",
                    "order_type": "MARKET", "protective_stop": "59000",
                    "targets": ["62500"], "strategy": "breakout",
                },
            )
            assert placed.status_code == 200
            assert placed.json()["placed"] is True

            with db.session_scope() as session:
                rows = session.scalars(
                    select(AuditLog).where(AuditLog.action == "order.submit")
                ).all()
                assert len(rows) == 1
        finally:
            set_settings(None)
