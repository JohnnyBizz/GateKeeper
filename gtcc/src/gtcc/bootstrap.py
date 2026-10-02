"""Production wiring.

Phase 1 ships one broker (paper) and one data source (recorded CSV).
Neither invents a price. If no recording is present the data adapter
reports itself unhealthy, ``/api/health`` returns degraded, and the
risk engine refuses orders for want of a quote — which is the correct
behaviour for a platform with no market data, and better than a
synthetic feed that looks like a market.
"""

from __future__ import annotations

import logging
from pathlib import Path

from gtcc.adapters.base import AdapterRegistry
from gtcc.adapters.paper import PaperBroker
from gtcc.adapters.replay import ReplayAdapter
from gtcc.config import Settings
from gtcc.domain.money import D
from gtcc.execution.paper_engine import FillModel, PaperFillEngine
from gtcc.logging_setup import log_event
from gtcc.risk.limits import RiskConfigError, RiskLimits, load_limits
from gtcc.runtime import TradingRuntime
from gtcc.storage import db
from gtcc.strategies.trend_continuation import TrendContinuation
from gtcc.storage.repositories import (
    ExecutionLatchRepository,
    RiskStateRepository,
    TradeJournalRepository,
)

logger = logging.getLogger("gtcc.bootstrap")


def load_risk_limits(settings: Settings) -> RiskLimits:
    """Read the owner's limits, or explain exactly what is missing."""
    try:
        return load_limits(settings.risk_config_path)
    except RiskConfigError as exc:
        log_event(logger, logging.ERROR, "risk configuration rejected", error=str(exc))
        raise


def build_runtime(
    settings: Settings,
    *,
    data_directory: Path | None = None,
    starting_cash: str = "100000",
    oanda_transport=None,
) -> TradingRuntime:
    """Assemble the runtime this deployment will use.

    *oanda_transport* is a test seam: it lets the suite exercise this
    exact wiring against a stub HTTP transport, so the bootstrap path is
    covered rather than only the adapters it builds. Production passes
    nothing and gets a real connection.
    """
    limits = load_risk_limits(settings)

    if settings.oanda_configured:
        # A practice account gives real prices and simulated funds,
        # which is what Phase 2 wants. Orders still go through the risk
        # engine; OANDA is only the venue at the far end.
        from gtcc.adapters.oanda import build_oanda

        data, broker = build_oanda(
            token=settings.oanda_token.get_secret_value(),
            account_id=settings.oanda_account_id,
            environment=settings.oanda_environment,
            deployment_allows_live=settings.allow_live_trading,
            timeout_seconds=settings.oanda_timeout_seconds,
            transport=oanda_transport,
        )
        data.client.limiter.per_second = settings.oanda_requests_per_second
        log_event(
            logger, logging.INFO, "using the OANDA adapter",
            environment=settings.oanda_environment,
            account_id=settings.oanda_account_id,
        )
    else:
        data = ReplayAdapter(
            directory=data_directory or settings.recordings_path
        )
        broker = PaperBroker(
            starting_cash=D(starting_cash),
            quote_source=data.get_quote,
            instrument_source=data.get_instrument,
            fill_engine=PaperFillEngine(FillModel()),
        )

    registry = AdapterRegistry()
    registry.register_data(data)
    registry.register_broker(broker)

    # Persisting the risk state is what makes a tripped breaker survive
    # a restart. Without a configured database there is nowhere to put
    # it, and the runtime says so rather than pretending.
    state_store = None
    latch_store = None
    journal_store = None
    try:
        db.get_engine()
        state_store = RiskStateRepository(db.session_scope)
        latch_store = ExecutionLatchRepository(db.session_scope)
        journal_store = TradeJournalRepository(db.session_scope)
    except RuntimeError:
        log_event(
            logger,
            logging.WARNING,
            "risk state will not be persisted",
            reason=(
                "database not configured before build_runtime; neither the "
                "risk tally nor the safety latch will survive a restart, and "
                "no setup will be journalled"
            ),
        )

    runtime = TradingRuntime(
        settings=settings,
        limits=limits,
        registry=registry,
        broker_name=broker.name,
        data_name=data.name,
        state_store=state_store,
        latch_store=latch_store,
        journal_store=journal_store,
    )
    # The shipped strategy, registered so it can be scanned and backtested.
    # It is UNTESTED, which the framework reads as "may not propose anything
    # in paper or live" — registering it does not enable it, it only makes it
    # possible to measure, which is the only way it could ever stop being
    # UNTESTED.
    runtime.strategies.register(TrendContinuation())
    log_event(
        logger,
        logging.INFO,
        "runtime built",
        broker=broker.name,
        data=data.name,
        mode=str(settings.mode),
        allow_live_trading=settings.allow_live_trading,
        limits_are_example=limits.is_example,
    )
    return runtime


def init_database(settings: Settings) -> None:
    db.configure(settings.database_dsn)
    db.create_all()
