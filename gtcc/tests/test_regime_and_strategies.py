"""Regime classification and the strategy framework.

The framework's job is to keep untested ideas away from money, so most
of what is tested here is refusal: which strategies are not allowed to
speak, and why. A strategy that proposes a trade is the easy case.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from gtcc.domain.enums import (
    AssetClass,
    Decision,
    Market,
    Regime,
    Timeframe,
    TradingMode,
)
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.money import D
from gtcc.features.indicators import directional_movement
from gtcc.features.regime import RegimeClassifier, RegimeReading, RegimeSettings
from gtcc.strategies.base import (
    Proposal,
    Strategy,
    StrategyContext,
    ValidationStatus,
)
from gtcc.strategies.registry import StrategyRegistry
from gtcc.strategies.trend_continuation import (
    TrendContinuation,
    TrendContinuationSettings,
)
from gtcc.structure.engine import StructureEngine
from tests.test_structure import bars_from, zigzag


@pytest.fixture
def classifier() -> RegimeClassifier:
    return RegimeClassifier()


@pytest.fixture
def spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="X", market=Market.STOCKS, asset_class=AssetClass.EQUITY,
        quote_currency="USD", tick_size=D("0.01"), lot_step=D("1"), min_qty=D("1"),
    )


def context_for(closes, spec, *, timeframe=Timeframe.H1, classifier=None, **kwargs):
    bars = bars_from(closes)
    structure = StructureEngine().analyse(bars)
    regime = (classifier or RegimeClassifier()).classify(bars, structure=structure, **kwargs)
    return StrategyContext(
        symbol="X", market=Market.STOCKS, timeframe=timeframe, instrument=spec,
        bars=bars, quote=None, structure=structure, regime=regime,
    )


class TestRegimeClassification:
    def test_a_one_way_market_is_trending_even_with_no_swings(self, classifier):
        """A clean straight line has no pivots at all, so the structure
        engine has no opinion and the direction has to come from the DI
        lines. An earlier version called this RANGING, which was
        exactly backwards."""
        up = classifier.classify(bars_from(list(range(100, 200))))
        down = classifier.classify(bars_from(list(range(200, 100, -1))))

        assert up.regime is Regime.TRENDING_UP
        assert down.regime is Regime.TRENDING_DOWN

    def test_a_zigzag_trend_is_read_from_the_swing_sequence(self, classifier):
        assert classifier.classify(bars_from(zigzag(legs=12))).regime is Regime.TRENDING_UP

    def test_a_chop_is_choppy(self, classifier):
        closes = [100 + (2 if i % 2 else -2) for i in range(120)]

        assert classifier.classify(bars_from(closes)).regime is Regime.CHOPPY

    def test_too_little_history_is_unknown_and_not_confident(self, classifier):
        reading = classifier.classify(bars_from(list(range(100, 110))))

        assert reading.regime is Regime.UNKNOWN
        assert reading.confident is False

    def test_an_imminent_event_overrides_the_technical_read(self, classifier):
        """Whatever the chart was doing is about to be interrupted."""
        reading = classifier.classify(
            bars_from(zigzag(legs=12)), minutes_to_high_impact_event=5
        )

        assert reading.regime is Regime.EVENT_RISK
        assert "overrides" in reading.rule

    def test_a_distant_event_does_not_override(self, classifier):
        reading = classifier.classify(
            bars_from(zigzag(legs=12)), minutes_to_high_impact_event=300
        )

        assert reading.regime is not Regime.EVENT_RISK

    def test_volatility_is_a_separate_axis_from_trend(self, classifier):
        """Collapsing both into one label throws away the half the
        strategy cared about."""
        closes = [100] * 120

        reading = classifier.classify(bars_from(closes))

        assert Regime.LOW_VOLATILITY in reading.all_regimes

    def test_the_reading_carries_its_evidence_and_thresholds(self, classifier):
        reading = classifier.classify(bars_from(zigzag(legs=12)))

        assert reading.evidence["adx"] is not None
        assert reading.evidence["plus_di"] is not None
        assert reading.settings["adx_trending"] == "25"
        assert reading.rule

    def test_the_thresholds_change_the_answer(self):
        """Which is why they travel with the reading."""
        closes = zigzag(legs=12)
        lenient = RegimeClassifier(RegimeSettings(adx_trending=D(10)))
        strict = RegimeClassifier(RegimeSettings(adx_trending=D(90), adx_choppy=D(85)))

        assert lenient.classify(bars_from(closes)).regime is Regime.TRENDING_UP
        assert strict.classify(bars_from(closes)).regime is not Regime.TRENDING_UP

    def test_the_same_bars_classify_the_same_way_twice(self, classifier):
        bars = bars_from(zigzag(legs=12))

        first = classifier.classify(bars)
        second = classifier.classify(bars)

        assert first.regime is second.regime
        assert first.evidence == second.evidence

    def test_permits_treats_an_unknown_regime_as_permitting_nothing(self):
        """A strategy gated on regime should not run when the regime is
        unknown: that is a guess, not a gate."""
        unknown = RegimeReading(regime=Regime.UNKNOWN, confident=False)

        assert unknown.permits([Regime.TRENDING_UP]) is False
        assert unknown.permits([]) is True  # no restriction declared


class TestDirectionalMovement:
    def test_the_di_lines_say_which_way_and_adx_does_not(self):
        up = list(range(100, 200))
        down = list(range(200, 100, -1))

        rising = directional_movement(bars_from(up), 14)
        falling = directional_movement(bars_from(down), 14)

        assert rising.plus_di[-1] > rising.minus_di[-1]
        assert falling.minus_di[-1] > falling.plus_di[-1]
        # Same strength, opposite directions.
        assert rising.adx[-1] > D(40) and falling.adx[-1] > D(40)


class TestTheValidationGate:
    """Section 25: nothing untested trades, even on paper."""

    def test_an_untested_strategy_cannot_run_in_paper(self, spec):
        context = context_for(zigzag(legs=14), spec)

        allowed, reason = TrendContinuation().may_run(context, TradingMode.PAPER)

        assert allowed is False
        assert "out-of-sample" in reason

    def test_an_untested_strategy_cannot_run_live(self, spec):
        context = context_for(zigzag(legs=14), spec)

        allowed, _ = TrendContinuation().may_run(context, TradingMode.LIVE)

        assert allowed is False

    def test_an_untested_strategy_returns_wait_rather_than_an_idea(self, spec):
        context = context_for(zigzag(legs=14), spec)

        proposal = TrendContinuation().propose(context, TradingMode.PAPER)

        assert proposal.decision is Decision.WAIT
        assert proposal.evidence["gate"] == "not_eligible"

    def test_only_paper_validated_may_run_live(self, spec):
        context = context_for(zigzag(legs=14), spec)
        strategy = TrendContinuation()

        strategy.validation = ValidationStatus.OUT_OF_SAMPLE
        assert strategy.may_run(context, TradingMode.PAPER)[0] is True
        assert strategy.may_run(context, TradingMode.LIVE)[0] is False

        strategy.validation = ValidationStatus.PAPER_VALIDATED
        assert strategy.may_run(context, TradingMode.LIVE)[0] is True

    def test_in_sample_success_is_not_enough_for_anything(self):
        """The weakest possible evidence, treated as such."""
        assert ValidationStatus.IN_SAMPLE.may_trade_paper is False
        assert ValidationStatus.IN_SAMPLE.may_trade_live is False

    def test_the_default_status_is_untested(self):
        class Fresh(Strategy):
            name = "fresh"

            def evaluate(self, context):  # pragma: no cover - never reached
                raise AssertionError("an untested strategy must not be evaluated")

        assert Fresh().validation is ValidationStatus.UNTESTED


class TestTheRegimeGate:
    def test_a_strategy_is_silent_outside_its_regimes(self, spec):
        chop = [100 + (2 if i % 2 else -2) for i in range(120)]
        context = context_for(chop, spec)
        strategy = TrendContinuation()
        strategy.validation = ValidationStatus.PAPER_VALIDATED

        allowed, reason = strategy.may_run(context, TradingMode.PAPER)

        assert allowed is False
        assert "CHOPPY" in reason

    def test_a_strategy_is_silent_on_the_wrong_timeframe(self, spec):
        context = context_for(zigzag(legs=14), spec, timeframe=Timeframe.M1)
        strategy = TrendContinuation()
        strategy.validation = ValidationStatus.PAPER_VALIDATED

        allowed, reason = strategy.may_run(context, TradingMode.PAPER)

        assert allowed is False
        assert "M1" in reason or "1m" in reason

    def test_event_risk_silences_a_strategy_not_built_for_it(self, spec):
        context = context_for(
            zigzag(legs=14), spec, minutes_to_high_impact_event=5
        )
        strategy = TrendContinuation()
        strategy.validation = ValidationStatus.PAPER_VALIDATED

        proposal = strategy.propose(context, TradingMode.PAPER)

        assert proposal.decision is Decision.WAIT
        assert proposal.evidence.get("gate") in ("event_risk", "not_eligible")


class TestProposalCoherence:
    """A strategy bug must not reach the risk engine disguised as a trade."""

    def _bad(self, **overrides) -> Proposal:
        base = dict(
            decision=Decision.LONG, strategy="x", rationale="y",
            entry=D(100), stop=D(98), targets=(D(104),),
        )
        base.update(overrides)
        return Proposal(**base)

    def test_a_coherent_long_passes(self):
        assert self._bad().coherent() == (True, "")

    def test_a_long_with_the_stop_above_the_entry_is_rejected(self):
        ok, problem = self._bad(stop=D(102)).coherent()

        assert ok is False
        assert "not below" in problem

    def test_a_long_with_a_target_below_the_entry_is_rejected(self):
        ok, problem = self._bad(targets=(D(96),)).coherent()

        assert ok is False
        assert "above its entry" in problem

    def test_a_short_with_the_stop_below_the_entry_is_rejected(self):
        ok, _ = self._bad(decision=Decision.SHORT, stop=D(98), targets=(D(96),)).coherent()

        assert ok is False

    def test_an_actionable_proposal_without_a_target_is_rejected(self):
        ok, problem = self._bad(targets=()).coherent()

        assert ok is False
        assert "at least one target" in problem

    def test_wait_needs_no_levels(self):
        assert Proposal.wait("x", "nothing here").coherent() == (True, "")

    def test_an_incoherent_proposal_is_converted_to_wait(self, spec):
        """The framework catches it, so the risk engine never sees it."""

        class Broken(Strategy):
            name = "broken"
            validation = ValidationStatus.PAPER_VALIDATED

            def evaluate(self, context):
                return Proposal(
                    decision=Decision.LONG, strategy="broken", rationale="bug",
                    entry=D(100), stop=D(105), targets=(D(110),),
                )

        context = context_for(zigzag(legs=14), spec)
        proposal = Broken().propose(context, TradingMode.PAPER)

        assert proposal.decision is Decision.WAIT
        assert proposal.evidence["gate"] == "incoherent"
        assert proposal.evidence["original_decision"] == "LONG"


class TestTrendContinuation:
    @pytest.fixture
    def enabled(self) -> TrendContinuation:
        strategy = TrendContinuation()
        strategy.validation = ValidationStatus.PAPER_VALIDATED
        return strategy

    def test_it_waits_when_the_two_trend_reads_disagree(self, spec, enabled):
        """A straight line gives TRENDING_UP from the DI lines and no
        structural trend at all. Requiring both is the point."""
        context = context_for(list(range(100, 200)), spec)

        proposal = enabled.propose(context, TradingMode.PAPER)

        assert proposal.decision is Decision.WAIT
        assert "structure reads" in proposal.rationale

    def test_it_waits_when_price_is_far_from_the_average(self, spec, enabled):
        """This setup buys pullbacks, not extensions."""
        closes = zigzag(legs=14) + [250, 280, 320]
        context = context_for(closes, spec)

        proposal = enabled.propose(context, TradingMode.PAPER)

        assert proposal.decision is Decision.WAIT

    def test_a_proposal_carries_its_levels_and_its_reasoning(self, spec, enabled):
        closes = zigzag(legs=20, step=4, amplitude=6)
        context = context_for(closes, spec)

        proposal = enabled.propose(context, TradingMode.PAPER)

        if proposal.is_actionable:
            assert proposal.entry and proposal.stop and proposal.targets
            assert proposal.coherent()[0]
            assert proposal.setup == "TREND_CONTINUATION_PULLBACK"
            assert "atr" in proposal.evidence
            assert proposal.evidence["settings"]["fast_ma"] == 20
        else:
            # A WAIT must still say why, in words a person can check.
            assert len(proposal.rationale) > 20

    def test_levels_land_on_the_tick_grid(self, spec, enabled):
        from gtcc.domain.pricing import is_on_tick

        closes = zigzag(legs=20, step=4, amplitude=6)
        context = context_for(closes, spec)

        proposal = enabled.propose(context, TradingMode.PAPER)

        if proposal.is_actionable:
            assert is_on_tick(proposal.stop, spec.tick_size)
            for target in proposal.targets:
                assert is_on_tick(target, spec.tick_size)

    def test_it_refuses_a_setup_whose_gross_reward_is_too_thin(self, spec):
        strategy = TrendContinuation(
            TrendContinuationSettings(min_gross_reward_risk=D(99))
        )
        strategy.validation = ValidationStatus.PAPER_VALIDATED
        context = context_for(zigzag(legs=20, step=4, amplitude=6), spec)

        proposal = strategy.propose(context, TradingMode.PAPER)

        assert proposal.decision is Decision.WAIT

    def test_it_waits_with_too_little_history(self, spec, enabled):
        context = context_for(zigzag(legs=3), spec)

        proposal = enabled.propose(context, TradingMode.PAPER)

        assert proposal.decision is Decision.WAIT


class TestTheRegistry:
    def test_it_reports_why_a_strategy_was_silent(self, spec):
        """"Nothing proposed anything" and "everything was disabled"
        look identical from outside, and only one is a market fact."""
        registry = StrategyRegistry()
        registry.register(TrendContinuation())
        context = context_for(zigzag(legs=14), spec)

        outcomes = registry.evaluate_all(context, TradingMode.PAPER)

        assert len(outcomes) == 1
        assert outcomes[0].eligible is False
        assert "out-of-sample" in outcomes[0].reason

    def test_duplicate_registration_is_refused(self):
        registry = StrategyRegistry()
        registry.register(TrendContinuation())

        with pytest.raises(ValueError, match="already registered"):
            registry.register(TrendContinuation())

    def test_an_unknown_strategy_lists_what_is_registered(self):
        registry = StrategyRegistry()
        registry.register(TrendContinuation())

        with pytest.raises(KeyError, match="trend_continuation"):
            registry.get("nonexistent")

    def test_actionable_excludes_waits(self, spec):
        registry = StrategyRegistry()
        registry.register(TrendContinuation())
        context = context_for(zigzag(legs=14), spec)

        assert registry.actionable(context, TradingMode.PAPER) == []
