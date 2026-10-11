"""Find trade ideas worth looking at, and show the case for and against.

The finder runs every setup over every symbol, then puts each pattern it
finds on trial. A pattern is only surfaced as a PICK when:

  - nothing disqualifies it (a reward to risk below your minimum after the
    spread, costs that eat the stop, your own risk limits refusing it, or a
    measured track record of losing on unseen data);
  - at least four independent DIRECTIONAL families of evidence support it;
  - no more than one directional family argues against it.

Most charts, most of the time, produce nothing. That is correct. A scan of
thirty symbols returning zero picks is the finder working, not failing, and
the result says how many patterns were found and why each fell short — so
"nothing worth taking" can be told apart from "nothing was checked".

Every pick carries its counter-evidence. A master trader's first question
about an idea is what is wrong with it; a list of reasons to buy with the
reasons not to quietly removed is a sales pitch.

The finder is advisory. It holds a market-data adapter and an optional
read-only `vet` callable that asks the risk engine for a dry-run verdict.
It has no broker and cannot reach one, which CI asserts. Taking a trade is
a decision a person makes on the chart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable, Sequence

from gtcc.adapters.base import MarketDataAdapter
from gtcc.adapters.errors import AdapterError, FeatureUnavailable
from gtcc.domain.enums import Decision, Market, OrderType, Side, Timeframe
from gtcc.domain.money import D, ZERO
from gtcc.domain.orders import OrderRequest
from gtcc.opportunities.analysis import HIGHER, Analysis, analyse
from gtcc.opportunities.evidence import (
    FAMILY_LABEL,
    Dossier,
    Evidence,
    Family,
    against,
    for_,
    neutral,
    unknown,
)
from gtcc.opportunities.setups import ALL_SETUPS, Candidate, Setup, Style, _levels, _px
from gtcc.opportunities.track_record import SetupRecord, Verdict
from gtcc.opportunities.tradingview import chart_url, is_exact
from gtcc.structure.engine import Trend

#: A dry-run risk check: takes an order request, returns the engine's verdict.
Vetter = Callable[[OrderRequest], object]
#: Looks up a measured record for (setup, symbol, timeframe).
RecordLookup = Callable[[str, str, str], SetupRecord | None]


@dataclass(frozen=True, slots=True)
class FinderSettings:
    timeframe: Timeframe = Timeframe.M15
    bar_limit: int = 300
    min_history: int = 120
    #: Independent directional families that must support an idea. The
    #: setup's own trigger is one of them, so four means three more besides.
    min_supporting_families: int = 4
    max_opposing_families: int = 1
    min_reward_risk: Decimal = D("1.5")
    #: Spread as a share of the stop distance.
    warn_spread_share: Decimal = D("0.10")
    max_spread_share: Decimal = D("0.20")
    #: A stop closer than this many ATRs sits inside normal bar-to-bar noise.
    min_stop_atr: Decimal = D("0.5")
    max_stop_atr: Decimal = D("4")
    near_misses: int = 5
    max_symbols: int = 60


@dataclass(frozen=True, slots=True)
class Pick:
    symbol: str
    timeframe: Timeframe
    venue: str
    candidate: Candidate
    dossier: Dossier
    net_reward_risk: Decimal | None
    track_record: SetupRecord | None
    chart_url: str
    exact_chart: bool
    approved_quantity: Decimal | None = None
    risk_at_stop: Decimal | None = None

    @property
    def measured_edge(self) -> bool:
        return (
            self.track_record is not None
            and self.track_record.verdict is Verdict.MEASURED_EDGE
        )

    @property
    def warnings(self) -> tuple[Evidence, ...]:
        """Practical evidence against that did not disqualify."""
        from gtcc.opportunities.evidence import DIRECTIONAL, Stance
        return tuple(
            item for item in self.dossier.items
            if item.stance is Stance.AGAINST
            and item.family not in DIRECTIONAL
            and not item.disqualifying
        )

    @property
    def headline(self) -> str:
        c = self.candidate
        d = self.dossier
        parts = [
            f"{len(d.supporting)} independent reasons for",
            f"{len(d.opposing)} against",
        ]
        if d.mixed:
            parts.append(f"{len(d.mixed)} mixed")
        if self.warnings:
            parts.append(f"{len(self.warnings)} practical warning(s)")
        return f"{self.symbol} {self.timeframe} {c.direction} — {c.label}: " + ", ".join(parts)

    @property
    def edge_label(self) -> str:
        if self.measured_edge:
            return "MEASURED EDGE"
        return "NO MEASURED EDGE — an idea read off the chart, not a proven setup"


@dataclass(frozen=True, slots=True)
class NearMiss:
    symbol: str
    candidate: Candidate
    dossier: Dossier
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Unread:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class FinderResult:
    picks: tuple[Pick, ...]
    near_misses: tuple[NearMiss, ...]
    unread: tuple[Unread, ...]
    symbols_requested: int
    symbols_analysed: int
    patterns_found: int
    settings: FinderSettings
    finished_at: datetime | None = None

    def summary(self) -> str:
        line = (
            f"{self.symbols_requested} symbol(s) requested, "
            f"{self.symbols_analysed} analysed, {self.patterns_found} pattern(s) "
            f"found, {len(self.picks)} met the bar"
        )
        if self.unread:
            line += f", {len(self.unread)} could not be read"
        if self.patterns_found and not self.picks:
            line += (
                ". Nothing cleared the evidence bar — which is the common "
                "case, and the finder working rather than failing"
            )
        elif self.symbols_analysed and not self.patterns_found:
            line += ". No setup was present on any chart"
        return line + "."


# -- evidence ----------------------------------------------------------------

def _trend_evidence(a: Analysis, c: Candidate) -> list[Evidence]:
    out: list[Evidence] = []
    wanted = Trend.UP if c.long else Trend.DOWN
    opposite = Trend.DOWN if c.long else Trend.UP
    tf = a.timeframe.value
    if a.structure.insufficient_history:
        out.append(unknown(Family.TREND, f"Not enough {tf} history to read swing structure."))
    elif a.structure.trend is wanted:
        out.append(for_(Family.TREND, f"{tf} swing structure is {wanted}: {a.structure.trend_rule}."))
    elif a.structure.trend is opposite:
        out.append(against(Family.TREND, f"{tf} swing structure is {opposite} — this trade runs against it."))
    else:
        out.append(neutral(Family.TREND, f"{tf} swing structure is {a.structure.trend}: no clear trend either way."))

    fast = a.ema_fast[-1] if a.ema_fast else None
    slow = a.ema_slow[-1] if a.ema_slow else None
    if "ema_alignment" in c.uses:
        # The trigger fired BECAUSE the averages are aligned; reporting the
        # alignment again here would be one fact counted twice.
        return out
    if fast is not None and slow is not None:
        aligned = fast > slow if c.long else fast < slow
        relation = "above" if fast > slow else "below"
        text = f"{tf} EMA20 {_px(a, fast)} is {relation} EMA50 {_px(a, slow)}"
        out.append(for_(Family.TREND, text + ".") if aligned else against(Family.TREND, text + " — against the trade."))
    return out


def _higher_timeframe_evidence(a: Analysis, c: Candidate) -> list[Evidence]:
    if not a.higher_bars or a.higher_structure is None or a.higher_timeframe is None:
        return [unknown(Family.HIGHER_TIMEFRAME, "No higher-timeframe data, so the bigger picture was not checked.")]
    htf = a.higher_timeframe.value
    out: list[Evidence] = []
    wanted = Trend.UP if c.long else Trend.DOWN
    opposite = Trend.DOWN if c.long else Trend.UP
    trend = a.higher_structure.trend
    if a.higher_structure.insufficient_history:
        out.append(unknown(Family.HIGHER_TIMEFRAME, f"Not enough {htf} history for swing structure."))
    elif trend is wanted:
        out.append(for_(Family.HIGHER_TIMEFRAME, f"{htf} swing structure is {wanted} — the bigger picture agrees."))
    elif trend is opposite:
        out.append(against(Family.HIGHER_TIMEFRAME, f"{htf} swing structure is {opposite} — trading against the bigger picture."))
    else:
        out.append(neutral(Family.HIGHER_TIMEFRAME, f"{htf} swing structure is {trend}: no higher-timeframe trend to lean on."))
    if a.higher_ema_fast is not None and a.higher_ema_slow is not None:
        aligned = (a.higher_ema_fast > a.higher_ema_slow) if c.long else (a.higher_ema_fast < a.higher_ema_slow)
        relation = "above" if a.higher_ema_fast > a.higher_ema_slow else "below"
        text = f"{htf} EMA20 is {relation} EMA50"
        out.append(for_(Family.HIGHER_TIMEFRAME, text + ".") if aligned else against(Family.HIGHER_TIMEFRAME, text + " — against the trade."))
    return out


def _momentum_evidence(a: Analysis, c: Candidate) -> list[Evidence]:
    out: list[Evidence] = []
    rsi = [v for v in a.rsi[-3:] if v is not None]
    if len(rsi) >= 2:
        now, before = rsi[-1], rsi[-2]
        if c.long and now >= 75:
            out.append(against(Family.MOMENTUM, f"RSI(14) {now:.1f}: already stretched upward; much of the move may be done."))
        elif not c.long and now <= 25:
            out.append(against(Family.MOMENTUM, f"RSI(14) {now:.1f}: already stretched downward; much of the move may be done."))
        elif (now > before) == c.long:
            out.append(for_(Family.MOMENTUM, f"RSI(14) turning {'up' if c.long else 'down'}: {before:.1f} → {now:.1f}."))
        else:
            out.append(against(Family.MOMENTUM, f"RSI(14) still moving {'down' if c.long else 'up'}: {before:.1f} → {now:.1f}."))
    hist = [v for v in a.macd_histogram[-2:] if v is not None]
    if len(hist) == 2:
        rising = hist[-1] > hist[-2]
        if rising == c.long:
            out.append(for_(Family.MOMENTUM, f"MACD histogram {'rising' if rising else 'falling'} — momentum turning the trade's way."))
        else:
            out.append(against(Family.MOMENTUM, f"MACD histogram {'rising' if rising else 'falling'} — momentum still against the trade."))
    if not out:
        out.append(unknown(Family.MOMENTUM, "Not enough history for RSI or MACD."))
    return out


def _strength_evidence(a: Analysis, c: Candidate) -> list[Evidence]:
    if a.adx is None or a.plus_di is None or a.minus_di is None:
        return [unknown(Family.TREND_STRENGTH, "Not enough history for ADX.")]
    aligned = (a.plus_di > a.minus_di) if c.long else (a.minus_di > a.plus_di)
    di = f"+DI {a.plus_di:.1f} / -DI {a.minus_di:.1f}"
    if c.style is Style.CONTINUATION:
        if a.adx >= 25 and aligned:
            return [for_(Family.TREND_STRENGTH, f"ADX {a.adx:.1f} with {di}: a strong trend in the trade's direction.")]
        if a.adx >= 20 and not aligned:
            return [against(Family.TREND_STRENGTH, f"ADX {a.adx:.1f} with {di}: the stronger push is the other way.")]
        return [neutral(Family.TREND_STRENGTH, f"ADX {a.adx:.1f}: no established trend strength yet ({di}).")]
    if a.adx < 20:
        return [for_(Family.TREND_STRENGTH, f"ADX {a.adx:.1f}: no strong trend to fight, which a reversal needs.")]
    if a.adx >= 25 and not aligned:
        return [against(Family.TREND_STRENGTH, f"ADX {a.adx:.1f} with {di}: this fades a strong trend.")]
    if a.adx >= 25 and aligned:
        return [for_(Family.TREND_STRENGTH, f"ADX {a.adx:.1f} with {di}: the reversal is back into the stronger trend.")]
    return [neutral(Family.TREND_STRENGTH, f"ADX {a.adx:.1f}: trend strength middling ({di}).")]


def _volume_evidence(a: Analysis, c: Candidate) -> list[Evidence]:
    note = (
        " (forex volume at a broker is a tick count — a proxy for activity, "
        "not traded size)"
        if a.instrument.market is Market.FOREX else ""
    )
    if not a.has_volume:
        return [unknown(Family.VOLUME, "This venue reports no volume, so participation was not checked.")]
    rv = a.relative_volume
    if rv is None:
        return [unknown(Family.VOLUME, "Not enough history for relative volume.")]
    if rv >= D("1.5"):
        return [for_(Family.VOLUME, f"Trigger bar volume {rv:.2f}× its 20-bar average: real participation{note}.")]
    if rv <= D("0.6"):
        return [against(Family.VOLUME, f"Trigger bar volume only {rv:.2f}× average: little participation behind it{note}.")]
    return [neutral(Family.VOLUME, f"Trigger bar volume {rv:.2f}× average: ordinary{note}.")]


def _location_evidence(a: Analysis, c: Candidate) -> list[Evidence]:
    out: list[Evidence] = []
    atr = a.atr or ZERO
    basis = c.parameters.get("target_basis")
    if basis == "objective_2r":
        out.append(neutral(
            Family.LOCATION,
            "No swing level beyond entry in the visible history — open space, "
            "which is also why the target is an assumed 2R rather than a level.",
        ))
    elif basis == "measured":
        # Only meaningful when the target was NOT chosen as the next level;
        # otherwise "nothing in the way" is true by construction.
        if c.long:
            blocking = [p for p in _levels(a, above=c.entry) if c.entry + atr / 4 < p < c.target]
        else:
            blocking = [p for p in _levels(a, below=c.entry) if c.target < p < c.entry - atr / 4]
        if blocking:
            level = blocking[0]
            share = abs(level - c.entry) / c.reward if c.reward > ZERO else ZERO
            kind = "resistance" if c.long else "support"
            out.append(against(
                Family.LOCATION,
                f"{kind.capitalize()} at {_px(a, level)} sits {share:.0%} of the way to the target — "
                "price may stall there first.",
            ))
        else:
            out.append(for_(Family.LOCATION, "No swing level between entry and target: a clear path."))

    if atr > ZERO:
        if c.long:
            floor = [p for p in _levels(a, below=c.entry)]
            if floor and (c.entry - floor[0]) <= atr:
                out.append(for_(Family.LOCATION, f"Entry sits {(c.entry - floor[0]) / atr:.2f} ATR above support at {_px(a, floor[0])} — buying near a floor, not mid-air."))
        else:
            ceiling = [p for p in _levels(a, above=c.entry)]
            if ceiling and (ceiling[0] - c.entry) <= atr:
                out.append(for_(Family.LOCATION, f"Entry sits {(ceiling[0] - c.entry) / atr:.2f} ATR below resistance at {_px(a, ceiling[0])} — selling near a ceiling, not mid-air."))
    if not out:
        out.append(neutral(Family.LOCATION, "Entry is not near a known level, and the target is the next level by construction."))
    return out


def _regime_evidence(a: Analysis, c: Candidate) -> list[Evidence]:
    regime = a.regime
    if not regime.confident:
        return [unknown(Family.TREND_STRENGTH, "The regime classifier had too little history to call the market.")]
    current = regime.all_regimes
    if any(r in c.intended_regimes for r in current):
        return [for_(Family.TREND_STRENGTH, f"Market reads as {', '.join(map(str, current))} — the conditions a {c.label.lower()} is built for.")]
    from gtcc.domain.enums import Regime
    trending = {Regime.TRENDING_UP, Regime.TRENDING_DOWN}
    if c.style is Style.REVERSAL and c.setup == "RANGE_FADE" and trending & set(current):
        return [against(Family.TREND_STRENGTH, f"Market reads as {regime.regime}: fading a range edge inside a trend.")]
    if c.style is Style.CONTINUATION:
        wrong = Regime.TRENDING_DOWN if c.long else Regime.TRENDING_UP
        if wrong in current:
            return [against(Family.TREND_STRENGTH, f"Market reads as {wrong}: a continuation the other way.")]
    return [neutral(Family.TREND_STRENGTH, f"Market reads as {', '.join(map(str, current))}: not what this setup is built for, nor against it.")]


def _track_record_evidence(c: Candidate, record: SetupRecord | None, symbol: str, tf: Timeframe) -> list[Evidence]:
    if record is None:
        return [unknown(
            Family.TRACK_RECORD,
            f"NO MEASURED TRACK RECORD for {c.label.lower()} on {symbol} {tf.value}. "
            "Nobody has checked whether this setup has made money here; this idea "
            "rests on the chart alone. Run `gtcc track-record` once recordings exist.",
        )]
    if record.verdict is Verdict.MEASURED_EDGE:
        return [for_(Family.TRACK_RECORD, f"Measured and it held up: {record.describe()}.")]
    if record.verdict is Verdict.NO_EDGE:
        return [against(
            Family.TRACK_RECORD,
            f"Measured and it LOST on data it was not built on: {record.describe()}.",
            disqualifying=True,
        )]
    return [unknown(Family.TRACK_RECORD, f"Measured, but too few trades to mean anything: {record.describe()}.")]


def _practical_evidence(
    a: Analysis, c: Candidate, settings: FinderSettings
) -> tuple[list[Evidence], Decimal | None]:
    out: list[Evidence] = []
    risk = c.risk
    spread = None
    if a.quote is not None and a.quote.ask > a.quote.bid:
        spread = a.quote.ask - a.quote.bid

    if spread is None:
        out.append(unknown(Family.COSTS, "No live quote, so the spread is unknown and not included in the reward to risk below."))
        net = c.gross_reward_risk
    else:
        share = spread / risk if risk > ZERO else D(1)
        net = (c.reward - spread) / (risk + spread) if (risk + spread) > ZERO else None
        if share > settings.max_spread_share:
            out.append(against(Family.COSTS, f"Spread {_px(a, spread)} is {share:.0%} of the stop distance — costs eat the trade.", disqualifying=True))
        elif share > settings.warn_spread_share:
            out.append(against(Family.COSTS, f"Spread {_px(a, spread)} is {share:.0%} of the stop distance — expensive for a stop this tight."))
        else:
            out.append(for_(Family.COSTS, f"Spread {_px(a, spread)} is {share:.0%} of the stop distance."))

    if net is None:
        out.append(unknown(Family.REWARD_RISK, "Reward to risk could not be computed."))
    elif net < settings.min_reward_risk:
        out.append(against(
            Family.REWARD_RISK,
            f"Reward to risk {net:.2f} to the target{' after the spread' if spread is not None else ''} — "
            f"below your {settings.min_reward_risk} minimum. The next level is too close to be worth the risk.",
            disqualifying=True,
        ))
    else:
        out.append(for_(Family.REWARD_RISK, f"Reward to risk {net:.2f}{' after the spread' if spread is not None else ''}: risking {_px(a, risk)} to make {_px(a, c.reward)}."))

    atr = a.atr
    if atr and atr > ZERO:
        stop_atr = risk / atr
        if stop_atr < settings.min_stop_atr:
            out.append(against(Family.VOLATILITY, f"Stop is only {stop_atr:.2f} ATR away — inside normal bar-to-bar noise, so likely to be hit by randomness."))
        elif stop_atr > settings.max_stop_atr:
            out.append(against(Family.VOLATILITY, f"Stop is {stop_atr:.2f} ATR away — wide, which means a small position for the same risk."))
        else:
            out.append(for_(Family.VOLATILITY, f"Stop is {stop_atr:.2f} ATR away — outside normal noise."))

    out.append(unknown(
        Family.EVENT_RISK,
        "No economic calendar is connected, so scheduled news — rate decisions, "
        "inflation, payrolls — was NOT checked. Look at a calendar before taking this.",
    ))
    return out, net


def _vet_evidence(vet: Vetter | None, a: Analysis, c: Candidate) -> tuple[list[Evidence], Decimal | None, Decimal | None]:
    if vet is None:
        return [unknown(Family.RISK_LIMITS, "Not checked against your risk limits.")], None, None
    request = OrderRequest(
        symbol=a.symbol,
        market=a.instrument.market,
        side=Side.BUY if c.long else Side.SELL,
        order_type=OrderType.MARKET,
        protective_stop=c.stop,
        targets=(c.target,),
        strategy=f"pick:{c.setup}",
    )
    try:
        verdict = vet(request)
    except Exception as exc:  # noqa: BLE001 - reported, never a pass
        return [unknown(Family.RISK_LIMITS, f"The risk check could not run: {exc}")], None, None
    if getattr(verdict, "allowed", False):
        qty = verdict.approved_quantity
        risk = getattr(verdict, "approved_risk", None)
        text = f"Your risk engine approves {qty} units"
        if risk is not None:
            text += f", losing about {risk:,.2f} if the stop is hit"
        return [for_(Family.RISK_LIMITS, text + ".")], qty, risk
    reasons = "; ".join(getattr(verdict, "reasons", ()) or ("refused",))
    # "The risk engine", not "your risk limits": it also refuses for reasons
    # that are not about risk appetite — no live price to size against, a
    # latched breaker — and naming the wrong cause sends the reader to the
    # wrong fix.
    return [against(Family.RISK_LIMITS, f"The risk engine refuses this trade: {reasons}.", disqualifying=True)], None, None


# -- the finder ---------------------------------------------------------------

class OpportunityFinder:
    def __init__(
        self,
        data: MarketDataAdapter,
        *,
        setups: Sequence[Setup] = ALL_SETUPS,
        settings: FinderSettings | None = None,
        records: RecordLookup | None = None,
        vet: Vetter | None = None,
    ) -> None:
        self.data = data
        self.setups = tuple(setups)
        self.settings = settings or FinderSettings()
        self.records = records
        self.vet = vet

    def find(self, symbols: Sequence[str], *, now: datetime | None = None) -> FinderResult:
        settings = self.settings
        picks: list[Pick] = []
        misses: list[NearMiss] = []
        unread: list[Unread] = []
        analysed = 0
        patterns = 0

        requested = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        for symbol in requested[settings.max_symbols:]:
            unread.append(Unread(symbol, f"over the {settings.max_symbols}-symbol limit for one scan; not looked at"))

        for symbol in requested[:settings.max_symbols]:
            analysis = self._analyse(symbol, unread)
            if analysis is None:
                continue
            analysed += 1
            for setup in self.setups:
                for candidate in setup.detect(analysis):
                    patterns += 1
                    outcome = self._judge(analysis, candidate)
                    if isinstance(outcome, Pick):
                        picks.append(outcome)
                    else:
                        misses.append(outcome)

        picks.sort(key=lambda p: (
            0 if p.measured_edge else 1,
            -len(p.dossier.supporting),
            len(p.dossier.opposing),
            -(p.net_reward_risk or ZERO),
            p.symbol,
        ))
        misses.sort(key=lambda m: (
            len(m.dossier.disqualifiers),
            -len(m.dossier.supporting),
            m.symbol,
        ))
        return FinderResult(
            picks=tuple(picks),
            near_misses=tuple(misses[: settings.near_misses]),
            unread=tuple(unread),
            symbols_requested=len(requested),
            symbols_analysed=analysed,
            patterns_found=patterns,
            settings=settings,
            finished_at=now,
        )

    def _analyse(self, symbol: str, unread: list[Unread]) -> Analysis | None:
        settings = self.settings
        tf = settings.timeframe
        try:
            instrument = self.data.get_instrument(symbol)
            bars = [b for b in self.data.get_bars(symbol, tf, limit=settings.bar_limit) if b.closed]
        except (FeatureUnavailable, KeyError) as exc:
            unread.append(Unread(symbol, f"not available from this venue: {exc}"))
            return None
        except AdapterError as exc:
            unread.append(Unread(symbol, f"the venue did not answer: {exc}"))
            return None
        if len(bars) < settings.min_history:
            unread.append(Unread(symbol, f"{len(bars)} closed bars; {settings.min_history} needed before drawing any conclusion"))
            return None

        higher_tf = HIGHER.get(tf)
        higher: list = []
        if higher_tf is not None and higher_tf is not tf:
            try:
                higher = [b for b in self.data.get_bars(symbol, higher_tf, limit=settings.bar_limit) if b.closed]
            except (FeatureUnavailable, KeyError, AdapterError):
                higher = []
        try:
            quote = self.data.get_quote(symbol)
        except (FeatureUnavailable, KeyError, AdapterError):
            quote = None

        return analyse(
            bars, instrument, tf, quote=quote,
            higher_bars=higher, higher_timeframe=higher_tf,
        )

    def _judge(self, a: Analysis, c: Candidate) -> Pick | NearMiss:
        settings = self.settings
        record = self.records(c.setup, a.symbol, settings.timeframe.value) if self.records else None

        practical, net = _practical_evidence(a, c, settings)
        vetted, qty, risk = _vet_evidence(self.vet, a, c)
        dossier = Dossier.of(
            list(c.trigger)
            + _trend_evidence(a, c)
            + _higher_timeframe_evidence(a, c)
            + _momentum_evidence(a, c)
            + _strength_evidence(a, c)
            + _volume_evidence(a, c)
            + _location_evidence(a, c)
            + _regime_evidence(a, c)
            + _track_record_evidence(c, record, a.symbol, settings.timeframe)
            + practical
            + vetted
        )

        reasons: list[str] = [f"{FAMILY_LABEL[d.family]}: {d.statement}" for d in dossier.disqualifiers]
        supporting = len(dossier.supporting)
        if supporting < settings.min_supporting_families:
            reasons.append(
                f"only {supporting} independent reason(s) in favour "
                f"({', '.join(FAMILY_LABEL[f] for f in dossier.supporting) or 'none'}); "
                f"{settings.min_supporting_families} needed"
            )
        if len(dossier.opposing) > settings.max_opposing_families:
            reasons.append(
                f"{len(dossier.opposing)} independent reasons against "
                f"({', '.join(FAMILY_LABEL[f] for f in dossier.opposing)})"
            )
        if reasons:
            return NearMiss(a.symbol, c, dossier, tuple(reasons))

        return Pick(
            symbol=a.symbol,
            timeframe=settings.timeframe,
            venue=self.data.name,
            candidate=c,
            dossier=dossier,
            net_reward_risk=net,
            track_record=record,
            chart_url=chart_url(a.symbol, settings.timeframe, self.data.name),
            exact_chart=is_exact(self.data.name),
            approved_quantity=qty,
            risk_at_stop=risk,
        )
