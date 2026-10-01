"""Render risk limits in money, not percentages.

The limits are stored as fractions because that is what the engine
computes with. Fractions are also the reason people sign off on numbers
they have not really considered: "0.5%" is abstract, and "five hundred
dollars, and you are stopped for the day after four of those" is a
decision somebody can actually make.

This exists so the owner can read what they are agreeing to without
opening a YAML file. It is a reporting surface and nothing more: it
never decides anything, and every number it shows is either arithmetic
on the configured limits or a figure the real risk engine produced.
"""

from __future__ import annotations

from decimal import Decimal

from gtcc.domain.enums import Market
from gtcc.domain.money import D
from gtcc.risk.limits import RiskLimits


def _money(value: Decimal, currency: str) -> str:
    return f"{currency} {value:,.2f}"


#: Every configured ceiling that constrains one position's FACE VALUE,
#: as a fraction of equity, with the name to show the owner. A leverage
#: cap belongs here because face value over equity *is* leverage for a
#: single position on a flat account.
def _face_value_ceilings(
    limits: RiskLimits, market: Market
) -> list[tuple[str, Decimal]]:
    ceilings = [
        ("position size cap", limits.position_notional_limit(market)),
        ("per-asset concentration cap", limits.asset_exposure_limit(market)),
        ("leverage cap", limits.leverage_limit(market)),
    ]
    market_cap = limits.market_exposure_limit(market)
    if market_cap is not None:
        ceilings.append((f"{market} exposure cap", market_cap))
    return ceilings


def binding_face_value_cap(
    limits: RiskLimits, market: Market
) -> tuple[str, Decimal]:
    """The ceiling that actually binds one position in *market*.

    Four separate limits each cap face value, and only the smallest of
    them is real. Reporting the one that was most recently edited — a
    300% override, say, while a 40% market exposure cap sits above it —
    tells the owner they have room they do not have.
    """
    return min(_face_value_ceilings(limits, market), key=lambda pair: pair[1])


def explain(limits: RiskLimits, *, equity: Decimal, currency: str = "USD") -> list[str]:
    """Plain-language lines describing what the limits allow."""
    equity = D(equity)
    per_trade = equity * limits.max_risk_per_trade
    daily = equity * limits.max_daily_loss
    weekly = equity * limits.max_weekly_loss
    drawdown = equity * limits.max_drawdown

    trades_to_daily = int(daily / per_trade) if per_trade > 0 else 0

    lines = [
        f"On an account of {_money(equity, currency)}:",
        "",
        f"  Each trade risks at most   {_money(per_trade, currency)}"
        f"   ({limits.max_risk_per_trade * 100:.2f}% of the account)",
        f"  A losing day stops you at  {_money(daily, currency)}"
        f"   ({limits.max_daily_loss * 100:.2f}%), which is about "
        f"{trades_to_daily} full losses",
        f"  A losing week stops you at {_money(weekly, currency)}"
        f"   ({limits.max_weekly_loss * 100:.2f}%)",
        f"  Live trading switches off  {_money(drawdown, currency)} below your best "
        f"ever balance ({limits.max_drawdown * 100:.2f}%)",
        f"  {limits.max_consecutive_losses} losses in a row also stops you, "
        "whatever the money says",
        "",
        f"  Never more than {limits.max_open_positions} positions open at once",
        "",
        f"  A trade is refused unless it can make {limits.min_reward_risk}x what it "
        "risks, after spread, fees and slippage",
        f"  A trade is refused if the spread is wider than {limits.max_spread_bps} "
        "basis points",
        f"  A stop closer than {limits.min_stop_distance_ticks} ticks is treated as "
        "noise and refused",
    ]

    if limits.event_blackout_minutes:
        lines.append(
            f"  No new short-term trade within {limits.event_blackout_minutes} minutes "
            "of a big scheduled release"
        )

    lines += [
        "",
        "  Largest single position by FACE VALUE, and the limit that decides it.",
        "  Face value is the size of the thing you are holding, not the money",
        "  you can lose — the per-trade risk limit above governs the loss.",
    ]
    for market in Market:
        name, fraction = binding_face_value_cap(limits, market)
        lines.append(
            f"    {market:8} up to {_money(equity * fraction, currency):>18}"
            f"   ({name})"
        )

    # A face-value cap and a risk budget are two different ceilings, and
    # whichever is tighter is the one that sizes the trade. Tight stops
    # need big positions to risk the full budget, so below some stop
    # distance the cap always wins and the trade risks LESS than the
    # configured per-trade limit. The owner cannot work that out from
    # the percentages, and without it they will wonder why their losses
    # are a fraction of what they signed off on.
    if per_trade > 0:
        lines += [
            "",
            "  Where the face-value cap takes over from your risk budget:",
            "  with a stop TIGHTER than this, the cap sizes the trade and you",
            "  risk less than your full budget. Wider, and the budget governs.",
        ]
        for market in Market:
            _, fraction = binding_face_value_cap(limits, market)
            cap = equity * fraction
            if cap <= 0:
                continue
            crossover = per_trade / cap * 100
            lines.append(
                f"    {market:8} stops inside {crossover:.2f}% of the entry price"
            )

    if limits.is_example:
        lines.append("")
        lines.append(
            "  THESE ARE THE SHIPPED EXAMPLE NUMBERS. The platform will refuse to "
            "load them until you have read them and said they are yours."
        )

    return lines


def worked_example(
    limits: RiskLimits, *, equity: Decimal, currency: str = "USD"
) -> list[str]:
    """One concrete trade, sized by the real engine.

    An earlier version of this divided the risk budget by the stop
    distance and reported the answer. That is what the budget implies,
    not what the platform allows: for a leveraged instrument a
    face-value, leverage or exposure ceiling usually binds first, and
    quoting the uncapped number would have told the owner they were
    about to trade a far larger position than they actually would.

    So this builds a real context and asks the engine. Every figure
    below comes back from the verdict, including the reason for a
    refusal — guessing at the cause in prose is how a help text ends up
    contradicting the code it describes.
    """
    from datetime import datetime, timezone

    from gtcc.data.quality import check_quote
    from gtcc.domain.enums import AssetClass, OrderType, Side, TradingMode
    from gtcc.domain.instruments import InstrumentSpec
    from gtcc.domain.market_data import Quote
    from gtcc.domain.orders import Account, OrderRequest
    from gtcc.risk.engine import RiskContext, RiskEngine
    from gtcc.risk.safety import initial_state
    from gtcc.risk.state import fresh_state

    equity = D(equity)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    entry = D("1.08500")
    stop = D("1.08200")
    target = D("1.09400")

    spec = InstrumentSpec(
        symbol="EUR_USD", market=Market.FOREX, asset_class=AssetClass.FOREX_SPOT,
        base_currency="EUR", quote_currency=currency, tick_size=D("0.00001"),
        pip_size=D("0.0001"), lot_step=D("1"), min_qty=D("1"), max_leverage=D("30"),
    )
    quote = Quote(
        symbol="EUR_USD", timestamp=now, bid=entry - D("0.00005"), ask=entry,
        bid_size=D("5000000"), ask_size=D("5000000"), received_at=now,
    )
    account = Account(
        account_id="illustration", currency=currency, equity=equity, cash=equity,
        buying_power=equity, mode=TradingMode.PAPER, reconciled_at=now,
    )
    verdict = RiskEngine().evaluate(
        OrderRequest(
            symbol="EUR_USD", market=Market.FOREX, side=Side.BUY,
            order_type=OrderType.MARKET, protective_stop=stop, targets=(target,),
            strategy="illustration",
        ),
        RiskContext(
            execution=initial_state(TradingMode.PAPER), account=account,
            instrument=spec, limits=limits,
            state=fresh_state("illustration", equity, now=now),
            data_quality=check_quote(quote, now=now), quote=quote,
            estimated_slippage_bps=D("1"), now=now,
        ),
    )

    lines = [
        "A worked example, EUR/USD, run through the real risk engine:",
        "",
        f"  Buy at {entry}, stop at {stop}, first target {target}.",
        f"  That is {(entry - stop) * 10000:.0f} pips of risk.",
        f"  Your per-trade budget is "
        f"{_money(equity * limits.max_risk_per_trade, currency)}.",
    ]

    if not verdict.allowed:
        lines.append("")
        lines.append("  The engine REFUSES this trade, because:")
        for check in verdict.failures:
            lines.append(f"    {check.code}: {check.detail}")
        return lines

    units = verdict.approved_quantity
    face = units * entry
    lines += [
        "",
        f"  The engine approves {units:,} units.",
        f"  Face value {_money(face, currency)}, which is {face / equity:.2f}x the "
        "account, funded on margin.",
    ]
    if verdict.approved_risk is not None:
        lines.append(
            f"  If the stop is hit you lose about "
            f"{_money(verdict.approved_risk, currency)}."
        )
    if verdict.reward_risk is not None:
        lines.append(
            f"  Net reward to risk after costs: {verdict.reward_risk.ratio:.2f}, "
            f"against a minimum of {limits.min_reward_risk}."
        )
    if verdict.binding_limits:
        tightest = verdict.binding_limits[-1]
        lines += [
            "",
            f"  The size was cut from what the risk budget asked for by "
            f"{tightest.code}:",
            f"    {tightest.detail}",
            "  That is the engine doing its job. It also means this trade loses",
            "  less than your per-trade budget if it fails, not more.",
        ]
    return lines
