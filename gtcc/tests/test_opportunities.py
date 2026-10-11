"""The opportunity finder: patterns on trial, with the case against kept.

The properties these tests pin, in order of how much they matter:

  1. One fact is never counted twice. Readings in one family count once;
     an input the trigger already used cannot vouch for the trigger again;
     practical facts (a good reward to risk) never count as evidence that
     price will move.
  2. Nothing is presented as more than it is. A setup with no measured
     track record says so on the pick itself; anything that could not be
     checked is listed as not checked rather than left out.
  3. The bar is real. Disqualifying evidence removes an idea whatever
     supports it, and a scan that finds nothing says it found nothing.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.adapters.base import AdapterHealth, Capability, MarketDataAdapter
from gtcc.adapters.errors import ConnectionUnhealthy, FeatureUnavailable
from gtcc.domain.enums import AssetClass, Decision, Market, Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote
from gtcc.domain.money import D
from gtcc.opportunities.analysis import analyse
from gtcc.opportunities.evidence import (
    DIRECTIONAL,
    Dossier,
    Evidence,
    Family,
    FamilyStance,
    Stance,
    against,
    for_,
    neutral,
    unknown,
)
from gtcc.opportunities.finder import FinderSettings, OpportunityFinder, Pick
from gtcc.opportunities.setups import (
    Breakout,
    FailedBreakout,
    LiquiditySweep,
    RangeFade,
    TrendPullback,
)
from gtcc.opportunities.tradingview import chart_url, is_exact, tradingview_symbol

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _spec(symbol: str = "EUR_USD") -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, market=Market.FOREX, asset_class=AssetClass.FOREX_SPOT,
        quote_currency="USD", base_currency="EUR", tick_size=D("0.00001"),
        pip_size=D("0.0001"), lot_step=D("1"), min_qty=D("1"), max_leverage=D("30"),
    )


def _bars(rows, tf: Timeframe = Timeframe.M15, symbol: str = "EUR_USD", volumes=None):
    step = timedelta(seconds=tf.seconds)
    n = len(rows)
    return [
        Bar(
            symbol=symbol, timeframe=tf, timestamp=NOW - step * (n - 1 - i),
            open=D(f"{o:.5f}"), high=D(f"{h:.5f}"), low=D(f"{l:.5f}"),
            close=D(f"{c:.5f}"),
            volume=D(str(volumes[i] if volumes else 1000)), closed=True,
        )
        for i, (o, h, l, c) in enumerate(rows)
    ]


def _wave(n=300, drift=0.00012, amp=0.0009, period=6.0, base=1.08):
    rows = []
    for i in range(n):
        p = base + drift * i + amp * math.sin(i / period)
        o = p - drift * 0.5
        rows.append((o, max(o, p) + 0.00025, min(o, p) - 0.00025, p))
    return rows


def _range_rows(n: int, low: float, high: float):
    """Bars oscillating inside [low, high] — a sideways market."""
    rows = []
    span = high - low
    for i in range(n):
        mid = low + span * (0.5 + 0.4 * math.sin(i / 2.3))
        rows.append((mid - span * 0.05, mid + span * 0.08, mid - span * 0.08, mid + span * 0.05))
    return rows


class TimeframeAdapter(MarketDataAdapter):
    """Serves different bars per timeframe, as a real venue does."""

    name = "oanda"
    capabilities = frozenset({Capability.BARS, Capability.QUOTES})

    def __init__(self, series, spec=None, quote=None, unlisted=(), failing=()):
        self.series = series
        self.spec = spec or _spec()
        self.quote = quote
        self.unlisted = set(unlisted)
        self.failing = set(failing)

    def get_instrument(self, symbol):
        if symbol in self.unlisted:
            raise FeatureUnavailable(self.name, "instrument", f"{symbol} not listed")
        if symbol in self.failing:
            raise ConnectionUnhealthy(self.name, "the venue returned 503")
        return self.spec

    def get_quote(self, symbol):
        if self.quote is None:
            raise FeatureUnavailable(self.name, "quotes", "none")
        return self.quote

    def get_bars(self, symbol, timeframe, *, limit=500, start=None, end=None):
        return list(self.series.get(timeframe, []))[-limit:]

    def health(self):
        return AdapterHealth.ok()


def _quote(price: Decimal, half_spread: str = "0.00002") -> Quote:
    h = D(half_spread)
    return Quote(
        symbol="EUR_USD", timestamp=NOW, bid=price - h, ask=price + h,
        bid_size=D("1000000"), ask_size=D("1000000"), received_at=NOW,
    )


# -- 1. one fact, counted once -----------------------------------------------

class TestEvidenceIsCountedByFamily:
    def test_several_readings_in_one_family_count_once(self):
        """RSI, MACD and stochastic agreeing is one observation, not three."""
        dossier = Dossier.of([
            for_(Family.MOMENTUM, "rsi up"),
            for_(Family.MOMENTUM, "macd up"),
            for_(Family.MOMENTUM, "stochastic up"),
        ])

        assert dossier.supporting == (Family.MOMENTUM,)

    def test_a_family_that_disagrees_with_itself_counts_neither_way(self):
        dossier = Dossier.of([
            for_(Family.MOMENTUM, "macd up"),
            against(Family.MOMENTUM, "rsi stretched"),
        ])

        assert dossier.family_stance(Family.MOMENTUM) is FamilyStance.MIXED
        assert dossier.supporting == ()
        assert dossier.opposing == ()
        assert dossier.mixed == (Family.MOMENTUM,)

    def test_practical_facts_never_count_as_directional_evidence(self):
        """A good reward to risk is not evidence that price will rise."""
        dossier = Dossier.of([
            for_(Family.REWARD_RISK, "3R"),
            for_(Family.COSTS, "tight spread"),
            for_(Family.VOLATILITY, "stop outside noise"),
        ])

        assert dossier.supporting == ()
        for family in (Family.REWARD_RISK, Family.COSTS, Family.VOLATILITY,
                       Family.EVENT_RISK, Family.RISK_LIMITS):
            assert family not in DIRECTIONAL

    def test_unknown_and_neutral_count_for_nothing_but_are_kept(self):
        dossier = Dossier.of([
            unknown(Family.VOLUME, "no volume"),
            neutral(Family.LOCATION, "mid-air"),
            for_(Family.TREND, "up"),
        ])

        assert dossier.supporting == (Family.TREND,)
        assert len(dossier.unknowns) == 1
        assert dossier.family_stance(Family.LOCATION) is FamilyStance.NEUTRAL

    def test_only_evidence_against_can_disqualify(self):
        with pytest.raises(ValueError):
            Evidence(Family.TREND, Stance.FOR, "x", disqualifying=True)

    def test_there_is_no_separate_regime_family(self):
        """Regime reads mostly off ADX — the same reading as trend strength.
        A separate family would count it twice."""
        assert "REGIME" not in Family.__members__


class TestATriggerCannotVouchForItself:
    def test_a_pullback_does_not_count_ema_alignment_twice(self):
        """The pullback fires BECAUSE the averages are aligned."""
        from gtcc.opportunities.finder import _trend_evidence

        bars = _bars(_wave()[:177])
        analysis = analyse(bars, _spec(), Timeframe.M15)
        candidates = TrendPullback().detect(analysis)
        assert candidates, "the fixture should contain a pullback"

        trend = _trend_evidence(analysis, candidates[0])

        assert not any("EMA20" in item.statement for item in trend)

    def test_the_same_reading_from_a_setup_that_did_not_use_it_counts(self):
        from gtcc.opportunities.finder import _trend_evidence
        from dataclasses import replace

        bars = _bars(_wave()[:177])
        analysis = analyse(bars, _spec(), Timeframe.M15)
        candidate = replace(TrendPullback().detect(analysis)[0], uses=frozenset())

        trend = _trend_evidence(analysis, candidate)

        assert any("EMA20" in item.statement for item in trend)


# -- the setups ------------------------------------------------------------

class TestEachSetupFiresOnItsPattern:
    def test_trend_pullback(self):
        analysis = analyse(_bars(_wave()[:177]), _spec(), Timeframe.M15)
        found = TrendPullback().detect(analysis)
        assert found and found[0].direction is Decision.LONG

    def test_breakout(self):
        rows = _range_rows(60, 1.0000, 1.0030)
        rows.append((1.0028, 1.0052, 1.0026, 1.0050))
        analysis = analyse(_bars(rows), _spec(), Timeframe.M15)

        found = Breakout().detect(analysis)

        assert found and found[0].direction is Decision.LONG
        assert found[0].target > found[0].entry > found[0].stop

    def test_range_fade(self):
        rows = _range_rows(60, 1.0000, 1.0060)
        rows.append((1.0005, 1.0012, 0.9999, 1.0010))
        analysis = analyse(_bars(rows), _spec(), Timeframe.M15)

        found = RangeFade().detect(analysis)

        assert found and found[0].direction is Decision.LONG
        assert found[0].parameters["target_basis"] == "measured"

    def test_failed_breakout(self):
        rows = _range_rows(40, 1.0000, 1.0030)
        rows += [
            (1.0028, 1.0045, 1.0027, 1.0042),   # breaks above
            (1.0040, 1.0041, 1.0020, 1.0024),   # back inside
            (1.0024, 1.0026, 1.0018, 1.0020),
            (1.0020, 1.0023, 1.0016, 1.0019),
            (1.0019, 1.0022, 1.0014, 1.0016),
            (1.0016, 1.0019, 1.0012, 1.0015),
        ]
        analysis = analyse(_bars(rows), _spec(), Timeframe.M15)

        found = FailedBreakout().detect(analysis)

        assert found and found[0].direction is Decision.SHORT

    def test_liquidity_sweep(self):
        rows = []
        price = 1.0100
        for i in range(30):                      # drift down to a clear low
            price -= 0.0003
            rows.append((price + 0.0002, price + 0.0004, price - 0.0001, price))
        low = price
        rows.append((low, low + 0.0002, low - 0.0006, low + 0.0001))   # the swing low
        for i in range(15):                      # rally away from it
            price = low + 0.0004 * (i + 1)
            rows.append((price - 0.0002, price + 0.0002, price - 0.0003, price))
        for i in range(8):                       # drift back toward it
            price -= 0.0006
            rows.append((price + 0.0002, price + 0.0003, price - 0.0002, price))
        swing_low = low - 0.0006
        rows.append((price, price + 0.0002, swing_low - 0.0004, swing_low + 0.0006))  # sweep
        analysis = analyse(_bars(rows), _spec(), Timeframe.M15)

        found = LiquiditySweep().detect(analysis)

        assert found and found[0].direction is Decision.LONG
        assert found[0].stop < D(f"{swing_low:.5f}")

    def test_nothing_fires_on_a_dead_flat_market(self):
        rows = [(1.0, 1.0001, 0.9999, 1.0)] * 200
        analysis = analyse(_bars(rows), _spec(), Timeframe.M15)

        for setup in (TrendPullback(), Breakout(), LiquiditySweep(), RangeFade(), FailedBreakout()):
            assert setup.detect(analysis) == [], setup.name


class TestLevelsAreOnTheGrid:
    def test_stop_and_target_sit_on_the_tick_and_round_toward_entry(self):
        analysis = analyse(_bars(_wave()[:177]), _spec(), Timeframe.M15)
        c = TrendPullback().detect(analysis)[0]
        tick = analysis.instrument.tick_size

        assert c.stop % tick == 0
        assert c.target % tick == 0
        assert c.stop < c.entry < c.target


class TestNoLookAhead:
    def test_an_unclosed_bar_changes_nothing(self):
        """A forming candle's close is not information yet."""
        bars = _bars(_wave()[:177])
        forming = Bar(
            symbol="EUR_USD", timeframe=Timeframe.M15,
            timestamp=bars[-1].timestamp + timedelta(minutes=15),
            open=D("1.2"), high=D("1.5"), low=D("0.9"), close=D("1.4"),
            volume=D("1"), closed=False,
        )

        with_forming = analyse(bars + [forming], _spec(), Timeframe.M15)
        without = analyse(bars, _spec(), Timeframe.M15)

        assert TrendPullback().detect(with_forming) == TrendPullback().detect(without)

    def test_a_backtested_setup_gives_the_same_trades_on_a_prefix(self):
        """The whole chain — analysis, detector, backtester — cannot read ahead."""
        from gtcc.backtest import BacktestSettings, Backtester
        from gtcc.opportunities.track_record import SetupStrategy
        from gtcc.risk.limits import parse_limits
        from tests.conftest import LIMITS_RAW

        bars = _bars(_wave(n=320))
        settings = BacktestSettings(symbol="EUR_USD", timeframe=Timeframe.M15, warmup_bars=80)
        bt = Backtester(limits=parse_limits(LIMITS_RAW))
        strategy = SetupStrategy(TrendPullback())

        full = bt.run(strategy, bars, _spec(), settings)
        prefix = bt.run(strategy, bars[:260], _spec(), settings)

        closed_in_prefix = [t for t in full.trades if t.exit_index < 259]
        for a, b in zip(closed_in_prefix, prefix.trades):
            assert (a.entry_index, a.entry_price, a.exit_price) == (b.entry_index, b.entry_price, b.exit_price)


# -- the finder ------------------------------------------------------------

def _finder(series=None, quote=None, **kwargs):
    m15 = _bars(_wave()[:177])
    h1 = _bars(_wave(n=200, drift=0.0004, amp=0.002, period=8.0), Timeframe.H1)
    data = TimeframeAdapter(
        series or {Timeframe.M15: m15, Timeframe.H1: h1},
        quote=quote if quote is not None else _quote(m15[-1].close),
        **{k: v for k, v in kwargs.items() if k in ("unlisted", "failing")},
    )
    settings = kwargs.get("settings") or FinderSettings(min_history=100)
    return OpportunityFinder(
        data, settings=settings,
        records=kwargs.get("records"), vet=kwargs.get("vet"),
    )


class TestTheBar:
    def test_too_few_independent_reasons_is_a_near_miss_naming_them(self):
        result = _finder().find(["EUR_USD"], now=NOW)

        assert result.picks == ()
        assert result.near_misses
        reason = " ".join(result.near_misses[0].reasons)
        assert "independent reason(s) in favour" in reason
        assert "4 needed" in reason

    def test_a_lower_bar_lets_the_same_idea_through(self):
        result = _finder(settings=FinderSettings(min_history=100, min_supporting_families=3)).find(["EUR_USD"], now=NOW)

        assert len(result.picks) == 1

    def test_disqualifying_costs_remove_an_idea_whatever_supports_it(self):
        m15 = _bars(_wave()[:177])
        wide = _quote(m15[-1].close, half_spread="0.00030")
        result = _finder(quote=wide, settings=FinderSettings(min_history=100, min_supporting_families=1)).find(["EUR_USD"], now=NOW)

        assert result.picks == ()
        assert any("costs eat the trade" in r for m in result.near_misses for r in m.reasons)

    def test_your_risk_engine_refusing_it_disqualifies_it(self):
        class Refused:
            allowed = False
            reasons = ("MAX_OPEN_POSITIONS: 5 already open",)

        result = _finder(
            vet=lambda request: Refused(),
            settings=FinderSettings(min_history=100, min_supporting_families=1),
        ).find(["EUR_USD"], now=NOW)

        assert result.picks == ()
        assert any("The risk engine refuses this trade" in r and "MAX_OPEN_POSITIONS" in r
                   for m in result.near_misses for r in m.reasons)

    def test_an_approving_risk_engine_sizes_the_pick(self):
        class Approved:
            allowed = True
            approved_quantity = D("12345")
            approved_risk = D("480.50")
            reasons = ()

        result = _finder(
            vet=lambda request: Approved(),
            settings=FinderSettings(min_history=100, min_supporting_families=3),
        ).find(["EUR_USD"], now=NOW)

        assert result.picks[0].approved_quantity == D("12345")
        assert result.picks[0].risk_at_stop == D("480.50")


class TestNothingIsPresentedAsMoreThanItIs:
    def _pick(self, **kwargs) -> Pick:
        result = _finder(settings=FinderSettings(min_history=100, min_supporting_families=3), **kwargs).find(["EUR_USD"], now=NOW)
        return result.picks[0]

    def test_an_unmeasured_setup_says_so_on_the_pick(self):
        pick = self._pick()

        assert not pick.measured_edge
        assert "NO MEASURED EDGE" in pick.edge_label
        assert any(
            "NO MEASURED TRACK RECORD" in item.statement
            for item in pick.dossier.unknowns
        )

    def test_news_risk_is_always_listed_as_not_checked(self):
        """Omitting it would read as checked-and-clear."""
        pick = self._pick()

        assert any(
            item.family is Family.EVENT_RISK and "NOT checked" in item.statement
            for item in pick.dossier.unknowns
        )

    def test_a_pick_without_a_risk_check_says_it_was_not_checked(self):
        pick = self._pick()

        assert any(item.family is Family.RISK_LIMITS for item in pick.dossier.unknowns)

    def test_the_headline_separates_directional_from_practical_concerns(self):
        pick = self._pick()

        assert "independent reasons for" in pick.headline
        assert "against" in pick.headline


class TestTrackRecordShapesTheOutcome:
    def _record(self, oos_r: str, oos_n: int = 15, is_n: int = 40, is_r: str = "0.4"):
        from gtcc.opportunities.track_record import SetupRecord

        return SetupRecord(
            setup="TREND_PULLBACK", symbol="EUR_USD", timeframe="15m",
            in_sample_trades=is_n, in_sample_expectancy_r=is_r,
            out_of_sample_trades=oos_n, out_of_sample_expectancy_r=oos_r,
            first_bar="2026-01-01T00:00:00+00:00", last_bar="2026-09-01T00:00:00+00:00",
            measured_at="2026-10-01T00:00:00+00:00", costs="assumed costs",
        )

    def test_a_setup_that_lost_on_held_out_data_is_disqualified(self):
        record = self._record("-0.3")
        result = _finder(
            records=lambda *a: record,
            settings=FinderSettings(min_history=100, min_supporting_families=1),
        ).find(["EUR_USD"], now=NOW)

        assert result.picks == ()
        assert any("LOST on data it was not built on" in r for m in result.near_misses for r in m.reasons)

    def test_a_measured_edge_counts_and_is_labelled(self):
        record = self._record("0.35")
        result = _finder(
            records=lambda *a: record,
            settings=FinderSettings(min_history=100, min_supporting_families=4),
        ).find(["EUR_USD"], now=NOW)

        assert len(result.picks) == 1, "the track record is the fourth family"
        assert result.picks[0].measured_edge
        assert result.picks[0].edge_label == "MEASURED EDGE"

    def test_too_few_trades_is_unknown_not_a_pass(self):
        from gtcc.opportunities.track_record import Verdict

        assert self._record("0.9", oos_n=3).verdict is Verdict.INSUFFICIENT
        assert self._record("0.9", is_n=5).verdict is Verdict.INSUFFICIENT

    def test_the_held_out_look_count_is_reported(self):
        from dataclasses import replace

        text = replace(self._record("0.3"), looks=3).describe()

        assert "looked at 3 times" in text
        assert "no longer truly held out" in text


class TestNothingIsDroppedSilently:
    def test_unreadable_symbols_are_reported(self):
        result = _finder(unlisted={"GHOST"}, failing={"DOWN"}).find(["EUR_USD", "GHOST", "DOWN"], now=NOW)

        reasons = {u.symbol: u.reason for u in result.unread}
        assert "not available" in reasons["GHOST"]
        assert "did not answer" in reasons["DOWN"]
        assert result.symbols_requested == 3

    def test_thin_history_is_refused_with_the_number(self):
        series = {Timeframe.M15: _bars(_wave()[:50])}
        result = _finder(series=series).find(["EUR_USD"], now=NOW)

        assert "50 closed bars" in result.unread[0].reason

    def test_symbols_over_the_limit_are_reported_not_ignored(self):
        settings = FinderSettings(min_history=100, max_symbols=1)
        result = _finder(settings=settings).find(["EUR_USD", "GBP_USD"], now=NOW)

        assert any(u.symbol == "GBP_USD" and "limit" in u.reason for u in result.unread)

    def test_finding_nothing_says_the_finder_worked(self):
        result = _finder().find(["EUR_USD"], now=NOW)

        assert "finder working rather than failing" in result.summary()


class TestTheChartLink:
    def test_oanda_symbols_map_to_tradingviews_naming(self):
        assert tradingview_symbol("EUR_USD", "oanda") == "OANDA:EURUSD"
        assert chart_url("EUR_USD", Timeframe.M15, "oanda") == (
            "https://www.tradingview.com/chart/?symbol=OANDA%3AEURUSD&interval=15"
        )

    def test_interval_codes(self):
        assert chart_url("X", Timeframe.H1, "oanda").endswith("interval=60")
        assert chart_url("X", Timeframe.H4, "oanda").endswith("interval=240")
        assert chart_url("X", Timeframe.D1, "oanda").endswith("interval=D")

    def test_an_unknown_venue_is_flagged_as_not_exact(self):
        """TradingView may chart a different exchange's prices."""
        assert tradingview_symbol("AAPL", "replay") == "AAPL"
        assert not is_exact("replay")
        assert is_exact("oanda")

    def test_every_pick_carries_a_link(self):
        result = _finder(settings=FinderSettings(min_history=100, min_supporting_families=3)).find(["EUR_USD"], now=NOW)

        assert result.picks[0].chart_url.startswith("https://www.tradingview.com/chart/")
        assert result.picks[0].exact_chart


class TestTheTrackRecordStore:
    def _record(self, **overrides):
        from gtcc.opportunities.track_record import SetupRecord

        base = dict(
            setup="BREAKOUT", symbol="EUR_USD", timeframe="15m",
            in_sample_trades=40, in_sample_expectancy_r="0.2",
            out_of_sample_trades=12, out_of_sample_expectancy_r="0.1",
            first_bar=None, last_bar=None, measured_at="2026-10-01T00:00:00+00:00",
            costs="assumed costs",
        )
        base.update(overrides)
        return SetupRecord(**base)

    def test_round_trip(self, tmp_path):
        from gtcc.opportunities.track_record import TrackRecordStore

        store = TrackRecordStore(tmp_path / "tr.json")
        store.save(self._record())

        found = store.lookup("BREAKOUT", "EUR_USD", "15m")

        assert found is not None and found.in_sample_trades == 40
        assert store.lookup("BREAKOUT", "GBP_USD", "15m") is None

    def test_every_re_measurement_counts_as_another_look(self, tmp_path):
        from gtcc.opportunities.track_record import TrackRecordStore

        store = TrackRecordStore(tmp_path / "tr.json")
        store.save(self._record())
        store.save(self._record())
        third = store.save(self._record())

        assert third.looks == 3
        assert store.lookup("BREAKOUT", "EUR_USD", "15m").looks == 3

    def test_a_corrupt_file_is_no_record_rather_than_a_good_one(self, tmp_path):
        from gtcc.opportunities.track_record import TrackRecordStore

        path = tmp_path / "tr.json"
        path.write_text("{ not json", encoding="utf-8")

        assert TrackRecordStore(path).lookup("BREAKOUT", "EUR_USD", "15m") is None

    def test_measuring_a_setup_on_a_recording(self):
        from gtcc.opportunities.track_record import SetupStrategy, measure
        from gtcc.risk.limits import parse_limits
        from gtcc.strategies.base import ValidationStatus
        from tests.conftest import LIMITS_RAW

        # A wide-swinging series: with a 5-pip ATR every tight-stop trade
        # fails the reward to risk after the default costs (see the test
        # below), and a fixture that produces no trades proves nothing.
        rows = _wave(n=900, drift=0.0012, amp=0.009, period=6.0)
        rows = [(o, h + 0.00225, l - 0.00225, c) for o, h, l, c in rows]
        record = measure(
            TrendPullback(), _bars(rows), _spec(), Timeframe.M15,
            parse_limits(LIMITS_RAW),
        )

        assert record.setup == "TREND_PULLBACK"
        assert record.in_sample_trades > 0
        assert record.first_bar is not None
        # Measuring is not promotion.
        assert SetupStrategy(TrendPullback()).validation is ValidationStatus.UNTESTED


class TestCostsDecideShortTimeframeTrades:
    """The finding this module turned up, pinned so it cannot be forgotten.

    On a series with an ATR like EUR/USD on 15 minutes, the backtester's
    generic default costs (2.5 bps per side) leave no tight-stop pullback
    able to clear a 1.5 reward to risk. At a low-cost broker's figures the
    same chart produces trades. Same rules, same prices: the cost assumption
    alone decides the answer, which is why a track record must state the
    costs it assumed and should use the venue's real spread where it can.
    """

    def test_the_same_chart_flips_on_the_cost_assumption(self):
        from gtcc.backtest import BacktestSettings, Backtester, BarCosts
        from gtcc.opportunities.track_record import SetupStrategy
        from gtcc.risk.limits import parse_limits
        from tests.conftest import LIMITS_RAW

        bars = _bars(_wave(n=600))
        bt = Backtester(limits=parse_limits(LIMITS_RAW))

        def trades(costs):
            return len(bt.run(
                SetupStrategy(TrendPullback()), bars, _spec(),
                BacktestSettings(symbol="EUR_USD", timeframe=Timeframe.M15,
                                 warmup_bars=60, costs=costs),
            ).trades)

        generic = trades(BarCosts())
        low_cost = trades(BarCosts(spread_bps=D("0.2"), slippage_bps=D("0.2"), commission_bps=D("0")))

        assert generic == 0
        assert low_cost > 0


class TestTheIdeasPage:
    """The page is where a person decides; it must not hide the case against."""

    @pytest.fixture
    def ideas_api(self, settings, runtime):
        from fastapi.testclient import TestClient

        from tests.conftest import _build_app, _sign_in
        from tests.support import OWNER_EMAIL, OWNER_PASSWORD

        m15 = _bars(_wave()[:177])
        h1 = _bars(_wave(n=200, drift=0.0004, amp=0.002, period=8.0), Timeframe.H1)
        adapter = TimeframeAdapter(
            {Timeframe.M15: m15, Timeframe.H1: h1}, quote=_quote(m15[-1].close)
        )
        runtime.registry.register_data(adapter)
        runtime.data_name = adapter.name

        class Approved:
            allowed = True
            approved_quantity = D("10000")
            approved_risk = D("48.00")
            reasons = ()

        runtime.evaluate = lambda request: Approved()  # dry run only
        client = TestClient(_build_app(settings, runtime))
        _sign_in(client, OWNER_EMAIL, OWNER_PASSWORD)
        return client

    def test_an_empty_page_lists_the_setups_it_looks_for(self, ideas_api):
        text = ideas_api.get("/ideas").text

        assert "Nothing scanned yet" in text
        assert "Trend pullback" in text and "Liquidity sweep reversal" in text

    def test_a_near_miss_is_shown_with_its_reason(self, ideas_api):
        text = ideas_api.get("/ideas?symbols=EUR_USD").text

        assert "Near misses" in text
        assert "independent reason(s) in favour" in text

    def test_the_page_says_where_its_data_comes_from(self, ideas_api):
        text = ideas_api.get("/ideas").text

        assert "not from TradingView" in text

    def test_needs_a_session(self, settings, runtime):
        from fastapi.testclient import TestClient

        from tests.conftest import _build_app

        client = TestClient(_build_app(settings, runtime), follow_redirects=False)
        assert client.get("/ideas").status_code in (302, 303, 307, 401, 403)


class TestARenderedPickShowsBothSides:
    def test_the_report_prints_for_against_and_not_checked(self):
        from gtcc.opportunities.report import render

        result = _finder(settings=FinderSettings(min_history=100, min_supporting_families=3)).find(["EUR_USD"], now=NOW)
        text = "\\n".join(render(result))

        assert "NO MEASURED EDGE" in text
        assert "    FOR:" in text
        assert "    NOT CHECKED:" in text
        assert "economic calendar" in text
        assert "tradingview.com/chart/" in text

    def test_by_stance_accepts_the_name_a_template_passes(self):
        dossier = Dossier.of([against(Family.TREND, "down")])

        assert dossier.by_stance("AGAINST") == dossier.by_stance(Stance.AGAINST)
        assert len(dossier.by_stance("AGAINST")) == 1
