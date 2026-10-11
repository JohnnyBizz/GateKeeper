"""Five setups, each a stated rule a person can check on the chart.

A "master trader" is not someone who sees more signals; it is someone who
knows exactly which few situations they trade, can say what invalidates
each one before entering, and passes on everything else. So each setup
here is a precise, written-down rule with its numbers attached, an
invalidation point (the stop) derived from the structure rather than from
a fixed distance, and a target at the next level where price is likely to
stall — not at a round number of R chosen to make the ratio look good.

Choosing the target as "the next opposing level" rather than "two times
the risk" is the honest choice and it costs picks: when the next level is
too close, the reward to risk fails and the idea is dropped. That is the
correct outcome. A trade whose obvious target is half a risk unit away is
not improved by drawing the target further out.

The setups:

  TREND_PULLBACK      buy a dip to the fast average in an uptrend (or sell
                      a rally in a downtrend)                 continuation
  BREAKOUT            first close beyond a tight range        continuation
  LIQUIDITY_SWEEP     a wick through a swing level that closes back inside,
                      taking the stops parked beyond it       reversal
  RANGE_FADE          a rejection at the edge of a range      reversal
  FAILED_BREAKOUT     a break of a range that closed back inside within
                      three bars, trapping the breakout traders  reversal

A detector looks only at the closed bars it is handed and the structure
already computed from them. It decides nothing about whether the idea is
good: it only says the pattern is present. Whether it is worth looking at
is the evidence dossier's job, and it is built separately so that a
pattern can never vouch for itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from gtcc.domain.enums import Decision, Regime
from gtcc.domain.money import D, ZERO
from gtcc.domain.pricing import normalise_stop, normalise_target
from gtcc.opportunities.analysis import Analysis
from gtcc.opportunities.evidence import Evidence, Family, for_, unknown
from gtcc.structure.engine import StructureKind


class Style(StrEnum):
    CONTINUATION = "CONTINUATION"
    REVERSAL = "REVERSAL"


@dataclass(frozen=True, slots=True)
class Candidate:
    setup: str
    label: str
    style: Style
    direction: Decision
    entry: Decimal
    stop: Decimal
    target: Decimal
    trigger: tuple[Evidence, ...]
    intended_regimes: tuple[Regime, ...]
    parameters: dict = field(default_factory=dict)
    #: Inputs the trigger already relied on. Evidence derived from them is
    #: not counted again: a fact the setup used to fire cannot then vouch
    #: for the setup a second time.
    uses: frozenset[str] = frozenset()

    @property
    def long(self) -> bool:
        return self.direction is Decision.LONG

    @property
    def risk(self) -> Decimal:
        return abs(self.entry - self.stop)

    @property
    def reward(self) -> Decimal:
        return abs(self.target - self.entry)

    @property
    def gross_reward_risk(self) -> Decimal | None:
        return self.reward / self.risk if self.risk > ZERO else None

    def coherent(self) -> bool:
        if self.risk <= ZERO or self.reward <= ZERO:
            return False
        if self.long:
            return self.stop < self.entry < self.target
        return self.target < self.entry < self.stop


def _px(analysis: Analysis, value: Decimal) -> str:
    """A price at the instrument's own precision, so it matches the chart."""
    tick = analysis.instrument.tick_size
    places = max(0, -tick.normalize().as_tuple().exponent)
    return f"{value:.{places}f}"


def _levels(analysis: Analysis, *, above: Decimal | None = None,
            below: Decimal | None = None) -> list[Decimal]:
    """Swing highs and resistance above a price, or swing lows and support
    below it, nearest first. Only confirmed structure is used."""
    if above is not None:
        kinds = (StructureKind.SWING_HIGH, StructureKind.RESISTANCE)
        prices = sorted(
            {d.price for d in analysis.structure.of(*kinds) if d.price > above}
        )
        return prices
    assert below is not None
    kinds = (StructureKind.SWING_LOW, StructureKind.SUPPORT)
    return sorted(
        {d.price for d in analysis.structure.of(*kinds) if d.price < below},
        reverse=True,
    )


def target_at_next_level(
    analysis: Analysis, entry: Decimal, stop: Decimal, long: bool
) -> tuple[Decimal, str]:
    """The next opposing level beyond a small buffer, or 2R when there is none.

    Levels within a quarter of an ATR of entry are skipped: a swing high one
    tick above the entry is the bar that just printed, not a destination.
    """
    atr = analysis.atr or ZERO
    buffer = atr * D("0.25")
    if long:
        levels = [p for p in _levels(analysis, above=entry) if p > entry + buffer]
    else:
        levels = [p for p in _levels(analysis, below=entry) if p < entry - buffer]
    if levels:
        return levels[0], f"the next {'swing high / resistance' if long else 'swing low / support'} at {_px(analysis, levels[0])}"
    risk = abs(entry - stop)
    objective = entry + risk * 2 if long else entry - risk * 2
    return objective, (
        "no opposing level in the visible history, so a 2R objective — "
        "an assumption rather than a level the market has shown"
    )


class Setup(ABC):
    name: str = "unnamed"
    label: str = ""
    style: Style = Style.CONTINUATION
    intended_regimes: tuple[Regime, ...] = ()
    #: One line a person can read on the page to know what this looks for.
    summary: str = ""
    #: Inputs the trigger is defined in terms of. See Candidate.uses.
    uses: frozenset[str] = frozenset()

    @abstractmethod
    def detect(self, analysis: Analysis) -> list[Candidate]:
        """Every instance of the pattern present on the last closed bar."""

    def _candidate(
        self, analysis: Analysis, *, long: bool, entry: Decimal, stop: Decimal,
        trigger: list[Evidence], parameters: dict,
        target: tuple[Decimal, str] | None = None,
    ) -> Candidate | None:
        if target is None:
            price, why = target_at_next_level(analysis, entry, stop, long)
            basis = "objective_2r" if why.startswith("no opposing level") else "next_level"
        else:
            price, why = target
            basis = "measured"
        # Recorded so the location check is not circular: when the target IS
        # the next level, "nothing stands between entry and target" is true by
        # construction and must not be reported as evidence.
        parameters = {**parameters, "target_basis": basis}
        # On the instrument's tick grid, rounded TOWARD entry: the stop can
        # only get tighter (risk never grows past what was sized for) and the
        # target only closer (the reward is never flattered by rounding).
        tick = analysis.instrument.tick_size
        try:
            stop = normalise_stop(stop, tick, entry=entry)
            price = normalise_target(price, tick, entry=entry)
        except ValueError:
            return None
        trigger = trigger + [for_(Family.STRUCTURE, f"Target: {why}.")]
        candidate = Candidate(
            setup=self.name, label=self.label, style=self.style,
            direction=Decision.LONG if long else Decision.SHORT,
            entry=entry, stop=stop, target=price,
            trigger=tuple(trigger),
            intended_regimes=self.intended_regimes,
            parameters=parameters,
            uses=self.uses,
        )
        return candidate if candidate.coherent() else None


class TrendPullback(Setup):
    name = "TREND_PULLBACK"
    label = "Trend pullback"
    style = Style.CONTINUATION
    intended_regimes = (Regime.TRENDING_UP, Regime.TRENDING_DOWN)
    summary = (
        "In an established trend, price dips back to the 20-period average "
        "without breaking the 50, then closes back in the trend's direction."
    )
    uses = frozenset({"ema_alignment"})

    TOUCH_ATR = D("0.25")
    HOLD_ATR = D("0.5")
    STOP_BUFFER_ATR = D("0.2")
    LOOKBACK = 5
    SLOPE_BARS = 5

    def detect(self, analysis: Analysis) -> list[Candidate]:
        atr = analysis.atr
        bars = analysis.bars
        if not atr or len(bars) < 60 or len(analysis.ema_slow) <= self.SLOPE_BARS:
            return []
        fast = analysis.ema_fast[-1]
        slow = analysis.ema_slow[-1]
        slow_then = analysis.ema_slow[-1 - self.SLOPE_BARS]
        if fast is None or slow is None or slow_then is None:
            return []

        recent = bars[-self.LOOKBACK:]
        last, prev = bars[-1], bars[-2]
        params = {
            "ema_fast": 20, "ema_slow": 50, "touch_atr": str(self.TOUCH_ATR),
            "hold_atr": str(self.HOLD_ATR), "lookback_bars": self.LOOKBACK,
        }
        out: list[Candidate] = []

        if fast > slow and slow > slow_then:
            dip = min(bar.low for bar in recent)
            touched = dip <= fast + atr * self.TOUCH_ATR
            held = dip > slow - atr * self.HOLD_ATR
            resumed = last.close > prev.close and last.close > fast
            # Fire at the turn, not throughout the leg that follows it: the
            # touch of the 20 must have happened on this bar or the one
            # before. Without this the rule fired on a third of all bars in a
            # test trend — every up-bar after a dip, which is not a signal.
            # (Requiring the previous CLOSE in the zone was tried first and
            # fired never: in a strong trend price wicks into the 20 rather
            # than closing there.)
            fresh = min(prev.low, last.low) <= fast + atr * self.TOUCH_ATR
            if touched and held and resumed and fresh:
                stop = dip - atr * self.STOP_BUFFER_ATR
                c = self._candidate(
                    analysis, long=True, entry=last.close, stop=stop,
                    parameters=params,
                    trigger=[for_(
                        Family.STRUCTURE,
                        f"Uptrend pullback: EMA20 {_px(analysis, fast)} is above "
                        f"EMA50 {_px(analysis, slow)} and the 50 is rising. In the "
                        f"last {self.LOOKBACK} bars price dipped to "
                        f"{_px(analysis, dip)}, back to the 20 without breaking "
                        f"the 50, and the last bar closed up at "
                        f"{_px(analysis, last.close)}, back above the 20.",
                    ), for_(
                        Family.STRUCTURE,
                        f"Invalidation: below the pullback low, "
                        f"{_px(analysis, stop)} ({self.STOP_BUFFER_ATR} ATR under it). "
                        "If price gets there the dip was not a dip.",
                    )],
                )
                if c:
                    out.append(c)

        if fast < slow and slow < slow_then:
            peak = max(bar.high for bar in recent)
            touched = peak >= fast - atr * self.TOUCH_ATR
            held = peak < slow + atr * self.HOLD_ATR
            resumed = last.close < prev.close and last.close < fast
            fresh = max(prev.high, last.high) >= fast - atr * self.TOUCH_ATR
            if touched and held and resumed and fresh:
                stop = peak + atr * self.STOP_BUFFER_ATR
                c = self._candidate(
                    analysis, long=False, entry=last.close, stop=stop,
                    parameters=params,
                    trigger=[for_(
                        Family.STRUCTURE,
                        f"Downtrend rally: EMA20 {_px(analysis, fast)} is below "
                        f"EMA50 {_px(analysis, slow)} and the 50 is falling. In the "
                        f"last {self.LOOKBACK} bars price rallied to "
                        f"{_px(analysis, peak)}, back to the 20 without breaking "
                        f"the 50, and the last bar closed down at "
                        f"{_px(analysis, last.close)}, back below the 20.",
                    ), for_(
                        Family.STRUCTURE,
                        f"Invalidation: above the rally high, "
                        f"{_px(analysis, stop)} ({self.STOP_BUFFER_ATR} ATR over it).",
                    )],
                )
                if c:
                    out.append(c)
        return out


class Breakout(Setup):
    name = "BREAKOUT"
    label = "Range breakout"
    style = Style.CONTINUATION
    intended_regimes = (Regime.RANGING, Regime.LOW_VOLATILITY, Regime.BREAKOUT)
    summary = (
        "The first close beyond a tight 20-bar range, targeting a move the "
        "size of the range."
    )

    RANGE_BARS = 20
    MAX_HEIGHT_ATR = D("4")
    CLEARANCE_ATR = D("0.1")

    def detect(self, analysis: Analysis) -> list[Candidate]:
        atr = analysis.atr
        bars = analysis.bars
        if not atr or len(bars) < self.RANGE_BARS + 2:
            return []
        window = bars[-self.RANGE_BARS - 1:-1]
        top = max(bar.high for bar in window)
        bottom = min(bar.low for bar in window)
        height = top - bottom
        if height <= ZERO or height > atr * self.MAX_HEIGHT_ATR:
            return []
        last, prev = bars[-1], bars[-2]
        mid = (top + bottom) / 2
        params = {
            "range_bars": self.RANGE_BARS, "max_height_atr": str(self.MAX_HEIGHT_ATR),
            "clearance_atr": str(self.CLEARANCE_ATR),
            "range_top": str(top), "range_bottom": str(bottom),
        }
        base = (
            f"{self.RANGE_BARS}-bar range {_px(analysis, bottom)}–{_px(analysis, top)}, "
            f"{height / atr:.1f} ATR tall — tight enough to be a base, not a trend"
        )
        holds = unknown(
            Family.STRUCTURE,
            "This is the bar that broke. Whether the break holds is not known "
            "yet: some first breaks fail back into the range, which is what the "
            "FAILED_BREAKOUT setup trades. How often they fail on this "
            "instrument is a question for the measured track record, not for "
            "a rule of thumb.",
        )
        out: list[Candidate] = []

        if last.close > top + atr * self.CLEARANCE_ATR and prev.close <= top:
            stop = max(last.low - atr * D("0.1"), mid)
            c = self._candidate(
                analysis, long=True, entry=last.close, stop=stop, parameters=params,
                target=(last.close + height, f"a measured move the height of the range, {_px(analysis, last.close + height)}"),
                trigger=[for_(
                    Family.STRUCTURE,
                    f"First close above the {base}: closed {_px(analysis, last.close)}, "
                    f"{(last.close - top) / atr:.2f} ATR clear of the top.",
                ), for_(
                    Family.STRUCTURE,
                    f"Invalidation: {_px(analysis, stop)} — back inside the range "
                    "past its midpoint means the break did not hold.",
                ), holds],
            )
            if c:
                out.append(c)

        if last.close < bottom - atr * self.CLEARANCE_ATR and prev.close >= bottom:
            stop = min(last.high + atr * D("0.1"), mid)
            c = self._candidate(
                analysis, long=False, entry=last.close, stop=stop, parameters=params,
                target=(last.close - height, f"a measured move the height of the range, {_px(analysis, last.close - height)}"),
                trigger=[for_(
                    Family.STRUCTURE,
                    f"First close below the {base}: closed {_px(analysis, last.close)}, "
                    f"{(bottom - last.close) / atr:.2f} ATR clear of the bottom.",
                ), for_(
                    Family.STRUCTURE,
                    f"Invalidation: {_px(analysis, stop)} — back inside the range "
                    "past its midpoint means the break did not hold.",
                ), holds],
            )
            if c:
                out.append(c)
        return out


class LiquiditySweep(Setup):
    name = "LIQUIDITY_SWEEP"
    label = "Liquidity sweep reversal"
    style = Style.REVERSAL
    intended_regimes = (Regime.RANGING, Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.CHOPPY)
    summary = (
        "A wick through a clean swing level that closes back inside — the "
        "stops parked beyond the level were taken and price reclaimed it."
    )

    STOP_BUFFER_ATR = D("0.1")

    def detect(self, analysis: Analysis) -> list[Candidate]:
        atr = analysis.atr
        bars = analysis.bars
        if not atr or len(bars) < 30:
            return []
        out: list[Candidate] = []
        last = bars[-1]

        for offset in (1, 2):
            sweep_index = len(bars) - offset
            sweep = bars[sweep_index]

            lows = [
                (i, p) for i, p in analysis.swing_prices(StructureKind.SWING_LOW)
                if i < sweep_index - 1 and sweep.low < p < sweep.close
            ]
            if lows and not any(c.long for c in out):
                i, level = max(lows)
                clean = all(bars[k].close > level for k in range(i + 1, sweep_index))
                if clean and last.close > level:
                    stop = sweep.low - atr * self.STOP_BUFFER_ATR
                    c = self._candidate(
                        analysis, long=True, entry=last.close, stop=stop,
                        parameters={"level": str(level), "set_bars_ago": len(bars) - 1 - i},
                        trigger=[for_(
                            Family.STRUCTURE,
                            f"Swept the swing low at {_px(analysis, level)} (set "
                            f"{len(bars) - 1 - i} bars ago, never closed below since): "
                            f"wicked to {_px(analysis, sweep.low)} and closed back above "
                            f"at {_px(analysis, sweep.close)}. The stops resting under "
                            "that low were taken and the level was reclaimed.",
                        ), for_(
                            Family.STRUCTURE,
                            f"Invalidation: below the sweep wick, {_px(analysis, stop)}.",
                        )],
                    )
                    if c:
                        out.append(c)

            highs = [
                (i, p) for i, p in analysis.swing_prices(StructureKind.SWING_HIGH)
                if i < sweep_index - 1 and sweep.close < p < sweep.high
            ]
            if highs and not any(not c.long for c in out):
                i, level = max(highs)
                clean = all(bars[k].close < level for k in range(i + 1, sweep_index))
                if clean and last.close < level:
                    stop = sweep.high + atr * self.STOP_BUFFER_ATR
                    c = self._candidate(
                        analysis, long=False, entry=last.close, stop=stop,
                        parameters={"level": str(level), "set_bars_ago": len(bars) - 1 - i},
                        trigger=[for_(
                            Family.STRUCTURE,
                            f"Swept the swing high at {_px(analysis, level)} (set "
                            f"{len(bars) - 1 - i} bars ago, never closed above since): "
                            f"wicked to {_px(analysis, sweep.high)} and closed back below "
                            f"at {_px(analysis, sweep.close)}. The stops resting over "
                            "that high were taken and the level was rejected.",
                        ), for_(
                            Family.STRUCTURE,
                            f"Invalidation: above the sweep wick, {_px(analysis, stop)}.",
                        )],
                    )
                    if c:
                        out.append(c)
        return out


class RangeFade(Setup):
    name = "RANGE_FADE"
    label = "Range edge rejection"
    style = Style.REVERSAL
    intended_regimes = (Regime.RANGING, Regime.CHOPPY)
    summary = (
        "A rejection bar at the edge of a 30-bar range, targeting the middle "
        "of the range."
    )

    RANGE_BARS = 30
    MIN_HEIGHT_ATR = D("2")
    MAX_HEIGHT_ATR = D("6")
    EDGE = D("0.25")

    def detect(self, analysis: Analysis) -> list[Candidate]:
        atr = analysis.atr
        bars = analysis.bars
        if not atr or len(bars) < self.RANGE_BARS + 2:
            return []
        window = bars[-self.RANGE_BARS - 1:-1]
        top = max(bar.high for bar in window)
        bottom = min(bar.low for bar in window)
        height = top - bottom
        if height < atr * self.MIN_HEIGHT_ATR or height > atr * self.MAX_HEIGHT_ATR:
            return []
        last = bars[-1]
        mid = bottom + height / 2
        position = (last.close - bottom) / height
        params = {"range_bars": self.RANGE_BARS, "top": str(top), "bottom": str(bottom)}
        out: list[Candidate] = []

        if (
            position <= self.EDGE
            and last.low <= bottom + atr * D("0.25")
            and last.close > last.open
            and last.close > bottom
        ):
            stop = min(bottom, last.low) - atr * D("0.3")
            c = self._candidate(
                analysis, long=True, entry=last.close, stop=stop, parameters=params,
                target=(mid, f"the middle of the range, {_px(analysis, mid)}"),
                trigger=[for_(
                    Family.STRUCTURE,
                    f"At the floor of the {self.RANGE_BARS}-bar range "
                    f"{_px(analysis, bottom)}–{_px(analysis, top)} "
                    f"({position:.0%} of the way up): tested {_px(analysis, last.low)} "
                    f"and closed up at {_px(analysis, last.close)}.",
                ), for_(
                    Family.STRUCTURE,
                    f"Invalidation: {_px(analysis, stop)}, under the range floor. A "
                    "close down there means the range is breaking, not holding.",
                )],
            )
            if c:
                out.append(c)

        if (
            position >= 1 - self.EDGE
            and last.high >= top - atr * D("0.25")
            and last.close < last.open
            and last.close < top
        ):
            stop = max(top, last.high) + atr * D("0.3")
            c = self._candidate(
                analysis, long=False, entry=last.close, stop=stop, parameters=params,
                target=(mid, f"the middle of the range, {_px(analysis, mid)}"),
                trigger=[for_(
                    Family.STRUCTURE,
                    f"At the ceiling of the {self.RANGE_BARS}-bar range "
                    f"{_px(analysis, bottom)}–{_px(analysis, top)} "
                    f"({position:.0%} of the way up): tested {_px(analysis, last.high)} "
                    f"and closed down at {_px(analysis, last.close)}.",
                ), for_(
                    Family.STRUCTURE,
                    f"Invalidation: {_px(analysis, stop)}, over the range ceiling.",
                )],
            )
            if c:
                out.append(c)
        return out


class FailedBreakout(Setup):
    name = "FAILED_BREAKOUT"
    label = "Failed breakout (trap)"
    style = Style.REVERSAL
    intended_regimes = (Regime.RANGING, Regime.CHOPPY, Regime.LOW_VOLATILITY)
    summary = (
        "A close beyond a range that was back inside within three bars — the "
        "traders who chased the break are now on the wrong side."
    )

    BASE_BARS = 20
    EVENT_BARS = 6
    RECLAIM_WITHIN = 3

    def detect(self, analysis: Analysis) -> list[Candidate]:
        atr = analysis.atr
        bars = analysis.bars
        need = self.BASE_BARS + self.EVENT_BARS
        if not atr or len(bars) < need:
            return []
        base = bars[-need:-self.EVENT_BARS]
        events = bars[-self.EVENT_BARS:]
        top = max(bar.high for bar in base)
        bottom = min(bar.low for bar in base)
        if top <= bottom:
            return []
        mid = (top + bottom) / 2
        last = bars[-1]
        clearance = atr * D("0.05")
        out: list[Candidate] = []

        for j, bar in enumerate(events[:-1]):
            if bar.close > top + clearance:
                back = next(
                    (k for k in range(j + 1, min(j + 1 + self.RECLAIM_WITHIN, len(events)))
                     if events[k].close < top),
                    None,
                )
                if back is not None and last.close < top:
                    extreme = max(b.high for b in events[j:])
                    stop = extreme + atr * D("0.1")
                    c = self._candidate(
                        analysis, long=False, entry=last.close, stop=stop,
                        parameters={"range_top": str(top), "range_bottom": str(bottom)},
                        target=(mid, f"the middle of the range it failed out of, {_px(analysis, mid)}"),
                        trigger=[for_(
                            Family.STRUCTURE,
                            f"Broke above the {self.BASE_BARS}-bar range high "
                            f"{_px(analysis, top)} (closed {_px(analysis, bar.close)}), "
                            f"then closed back inside within {back - j} bar(s). Latest "
                            f"close {_px(analysis, last.close)} is back below the level: "
                            "the buyers who chased the break are trapped above.",
                        ), for_(
                            Family.STRUCTURE,
                            f"Invalidation: above the failed break's high, {_px(analysis, stop)}.",
                        )],
                    )
                    if c:
                        out.append(c)
                break

        for j, bar in enumerate(events[:-1]):
            if bar.close < bottom - clearance:
                back = next(
                    (k for k in range(j + 1, min(j + 1 + self.RECLAIM_WITHIN, len(events)))
                     if events[k].close > bottom),
                    None,
                )
                if back is not None and last.close > bottom:
                    extreme = min(b.low for b in events[j:])
                    stop = extreme - atr * D("0.1")
                    c = self._candidate(
                        analysis, long=True, entry=last.close, stop=stop,
                        parameters={"range_top": str(top), "range_bottom": str(bottom)},
                        target=(mid, f"the middle of the range it failed out of, {_px(analysis, mid)}"),
                        trigger=[for_(
                            Family.STRUCTURE,
                            f"Broke below the {self.BASE_BARS}-bar range low "
                            f"{_px(analysis, bottom)} (closed {_px(analysis, bar.close)}), "
                            f"then closed back inside within {back - j} bar(s). Latest "
                            f"close {_px(analysis, last.close)} is back above the level: "
                            "the sellers who chased the break are trapped below.",
                        ), for_(
                            Family.STRUCTURE,
                            f"Invalidation: below the failed break's low, {_px(analysis, stop)}.",
                        )],
                    )
                    if c:
                        out.append(c)
                break
        return out


ALL_SETUPS: tuple[Setup, ...] = (
    TrendPullback(),
    Breakout(),
    LiquiditySweep(),
    RangeFade(),
    FailedBreakout(),
)

SETUPS_BY_NAME: dict[str, Setup] = {setup.name: setup for setup in ALL_SETUPS}
