"""Shared test doubles.

These live in their own module rather than in ``conftest.py`` because
pytest imports a conftest under its own module name. A test that then
does ``from tests.conftest import FixtureMisuse`` gets a *second* class
object, and ``pytest.raises`` against it silently fails to match the
one actually raised. One importable module, one class.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from gtcc.adapters.base import AdapterHealth, Capability, MarketDataAdapter
from gtcc.adapters.errors import FeatureUnavailable
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Quote

NOW = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "a-sufficiently-long-password"
VIEWER_EMAIL = "viewer@example.com"
VIEWER_PASSWORD = "another-long-enough-password"


class FixtureMisuse(AssertionError):
    """A test asked a fake for something it was never set up to answer.

    Raised rather than returning a plausible default. A fake that
    answers every question is a fake that can make a broken test pass.
    """



class StrictDataAdapter(MarketDataAdapter):
    """A data adapter that answers only for symbols it was given.

    Every failure mode a test might want is a switch rather than a
    separate class, so a test reads as "this adapter, with the broker
    down" instead of hiding the condition inside a bespoke double.
    """

    name = "strict-test-data"
    capabilities = frozenset({Capability.QUOTES, Capability.BARS})

    def __init__(
        self,
        *,
        instruments: dict[str, InstrumentSpec],
        quotes: dict[str, Quote],
        healthy: bool = True,
        health_detail: str = "test data adapter",
        quote_error: Exception | None = None,
        instrument_error: Exception | None = None,
        quote_age: timedelta | None = None,
        unlisted: set[str] | None = None,
        now: datetime = NOW,
    ) -> None:
        self._instruments = instruments
        self._quotes = quotes
        self.healthy = healthy
        self.health_detail = health_detail
        self.quote_error = quote_error
        self.instrument_error = instrument_error
        self.quote_age = quote_age
        #: Symbols this venue genuinely does not offer. Asking for one
        #: raises FeatureUnavailable, exactly as production would. Any
        #: OTHER unknown symbol is a test that forgot to set itself up,
        #: and that raises FixtureMisuse instead of quietly answering.
        self.unlisted = unlisted or {"NOSUCHTHING"}
        self.now = now
        #: Every symbol asked for, so a test can assert what was read.
        self.calls: list[tuple[str, str]] = []

    def get_instrument(self, symbol: str) -> InstrumentSpec:
        self.calls.append(("get_instrument", symbol))
        if self.instrument_error is not None:
            raise self.instrument_error
        if symbol in self.unlisted:
            raise FeatureUnavailable(
                self.name, "instrument specification",
                f"{symbol} is not listed on this venue",
            )
        try:
            return self._instruments[symbol]
        except KeyError:
            raise FixtureMisuse(
                f"the test asked for an instrument spec for {symbol!r}, which this "
                f"fixture was not given. Known: {sorted(self._instruments)}. "
                "Returning a stand-in here is how an AAPL order gets sized "
                "against a Bitcoin contract."
            ) from None

    def get_quote(self, symbol: str) -> Quote:
        self.calls.append(("get_quote", symbol))
        if self.quote_error is not None:
            raise self.quote_error
        if symbol in self.unlisted:
            raise FeatureUnavailable(self.name, "quotes", f"{symbol} is not listed")
        try:
            quote = self._quotes[symbol]
        except KeyError:
            raise FixtureMisuse(
                f"the test asked for a quote for {symbol!r}, which this fixture "
                f"was not given. Known: {sorted(self._quotes)}."
            ) from None
        if self.quote_age is not None:
            quote = replace(quote, timestamp=self.now - self.quote_age)
        return quote

    def get_bars(self, symbol, timeframe, *, limit=500, start=None, end=None):
        self.calls.append(("get_bars", symbol))
        return []

    def health(self) -> AdapterHealth:
        return (
            AdapterHealth.ok(self.health_detail)
            if self.healthy
            else AdapterHealth.down(self.health_detail)
        )
