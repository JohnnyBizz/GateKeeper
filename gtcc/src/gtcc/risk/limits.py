"""Risk limits — the owner's numbers, not ours.

Specification section 43: "Require configuration for financial risk
parameters rather than pretending one set of numbers is appropriate for
every user." So every field here is mandatory. There is no default
risk-per-trade, because a default would be a recommendation, and this
platform is not qualified to make one.

``config/risk.example.yaml`` exists to show the shape of the file and is
labelled an example throughout. :func:`load_limits` refuses to read it
unless the caller explicitly says it is doing so for a demonstration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

from gtcc.domain.enums import Market
from gtcc.domain.money import ZERO, D

EXAMPLE_MARKER = "THIS_IS_AN_EXAMPLE_NOT_FINANCIAL_ADVICE"


class RiskConfigError(ValueError):
    """The risk configuration is missing, malformed or self-contradictory."""


def _pct(raw: Any, name: str) -> Decimal:
    """Read a percentage, expressed 0-100, and return it as a fraction."""
    if raw is None:
        raise RiskConfigError(f"risk limit {name!r} is required and has no default")
    value = D(raw)
    if value < ZERO or value > D(100):
        raise RiskConfigError(f"risk limit {name!r} must be between 0 and 100, got {value}")
    return value / D(100)


def _exposure_pct(raw: Any, name: str) -> Decimal:
    """A notional or exposure ceiling, expressed 0-10000 percent.

    Unlike a loss limit these legitimately exceed 100. A futures or
    margined crypto position carries face value well above the equity
    behind it, so capping notional at one times equity would forbid the
    instrument rather than size it.
    """
    if raw is None:
        raise RiskConfigError(f"risk limit {name!r} is required and has no default")
    value = D(raw)
    if value < ZERO or value > D(10000):
        raise RiskConfigError(f"risk limit {name!r} must be between 0 and 10000, got {value}")
    return value / D(100)


def _positive_int(raw: Any, name: str) -> int:
    if raw is None:
        raise RiskConfigError(f"risk limit {name!r} is required and has no default")
    value = int(raw)
    if value < 0:
        raise RiskConfigError(f"risk limit {name!r} must not be negative")
    return value


@dataclass(frozen=True, slots=True)
class MarketOverride:
    """Per-market replacements for the limits that cannot be universal.

    A notional cap of 25% of equity is sensible for cash equities and
    makes listed futures untradeable: one ES contract is a quarter of a
    million dollars of face value against a few thousand of margin.
    Rather than loosen the global cap for everything, a market may
    carry its own.
    """

    max_position_notional: Decimal | None = None
    max_leverage: Decimal | None = None
    #: Concentration has to move with notional. Allowing a single
    #: futures position at 600% of equity while capping total exposure
    #: to that symbol at 25% is not conservative, it is incoherent.
    max_exposure_per_asset: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """All limits are fractions of equity unless the name says otherwise."""

    # -- per trade ---------------------------------------------------------
    max_risk_per_trade: Decimal
    max_position_notional: Decimal
    max_leverage: Decimal
    min_reward_risk: Decimal
    min_stop_distance_ticks: int
    max_spread_bps: Decimal
    max_slippage_bps: Decimal

    # -- per day, week, life ------------------------------------------------
    max_daily_loss: Decimal
    max_weekly_loss: Decimal
    max_drawdown: Decimal
    max_consecutive_losses: int

    # -- concurrency and concentration --------------------------------------
    max_open_positions: int
    max_exposure_per_asset: Decimal
    max_correlated_exposure: Decimal
    max_sector_exposure: Decimal
    max_market_exposure: Mapping[Market, Decimal] = field(default_factory=dict)
    #: Markets whose leveraged arithmetic needs its own ceilings.
    market_overrides: Mapping[Market, MarketOverride] = field(default_factory=dict)

    # -- scheduled events ----------------------------------------------------
    #: No new short-dated trade within this many minutes of a high-impact
    #: event, unless the strategy declares itself an event strategy.
    event_blackout_minutes: int = 0
    event_blackout_applies_above_timeframe_seconds: int = 3600

    #: Set when the limits came from the shipped example file.
    is_example: bool = False

    def __post_init__(self) -> None:
        if self.min_reward_risk < ZERO:
            raise RiskConfigError("min_reward_risk must not be negative")
        if self.max_leverage <= ZERO:
            raise RiskConfigError("max_leverage must be positive")
        if self.max_daily_loss > self.max_drawdown and self.max_drawdown > ZERO:
            raise RiskConfigError(
                "max_daily_loss exceeds max_drawdown: the daily breaker could "
                "never fire before the drawdown breaker, which is almost "
                "certainly not what was meant"
            )

    def market_exposure_limit(self, market: Market) -> Decimal | None:
        return self.max_market_exposure.get(market)

    def position_notional_limit(self, market: Market) -> Decimal:
        override = self.market_overrides.get(market)
        if override is not None and override.max_position_notional is not None:
            return override.max_position_notional
        return self.max_position_notional

    def leverage_limit(self, market: Market) -> Decimal:
        override = self.market_overrides.get(market)
        if override is not None and override.max_leverage is not None:
            return override.max_leverage
        return self.max_leverage

    def asset_exposure_limit(self, market: Market) -> Decimal:
        override = self.market_overrides.get(market)
        if override is not None and override.max_exposure_per_asset is not None:
            return override.max_exposure_per_asset
        if override is not None and override.max_position_notional is not None:
            # A market that raised its notional ceiling and said nothing
            # about concentration gets the larger of the two, so the two
            # limits cannot contradict each other.
            return max(self.max_exposure_per_asset, override.max_position_notional)
        return self.max_exposure_per_asset


def parse_limits(raw: Mapping[str, Any], *, is_example: bool = False) -> RiskLimits:
    per_trade = raw.get("per_trade") or {}
    drawdown = raw.get("drawdown") or {}
    concentration = raw.get("concentration") or {}
    events = raw.get("events") or {}

    overrides: dict[Market, MarketOverride] = {}
    for name, block in (raw.get("market_overrides") or {}).items():
        try:
            market = Market(str(name).upper())
        except ValueError as exc:
            raise RiskConfigError(f"unknown market {name!r} in market_overrides") from exc
        block = block or {}
        notional = block.get("max_position_notional_pct")
        leverage = block.get("max_leverage")
        overrides[market] = MarketOverride(
            max_position_notional=(
                _exposure_pct(notional, f"market_overrides.{name}.max_position_notional_pct")
                if notional is not None
                else None
            ),
            max_leverage=D(leverage) if leverage is not None else None,
            max_exposure_per_asset=(
                _exposure_pct(
                    block["max_exposure_per_asset_pct"],
                    f"market_overrides.{name}.max_exposure_per_asset_pct",
                )
                if block.get("max_exposure_per_asset_pct") is not None
                else None
            ),
        )

    market_exposure: dict[Market, Decimal] = {}
    for name, value in (concentration.get("max_market_exposure_pct") or {}).items():
        try:
            market = Market(str(name).upper())
        except ValueError as exc:
            raise RiskConfigError(f"unknown market {name!r} in max_market_exposure_pct") from exc
        market_exposure[market] = _exposure_pct(value, f"max_market_exposure_pct.{name}")

    return RiskLimits(
        max_risk_per_trade=_pct(per_trade.get("max_risk_pct"), "per_trade.max_risk_pct"),
        max_position_notional=_exposure_pct(
            per_trade.get("max_position_notional_pct"), "per_trade.max_position_notional_pct"
        ),
        max_leverage=D(_require(per_trade.get("max_leverage"), "per_trade.max_leverage")),
        min_reward_risk=D(_require(per_trade.get("min_reward_risk"), "per_trade.min_reward_risk")),
        min_stop_distance_ticks=_positive_int(
            per_trade.get("min_stop_distance_ticks"), "per_trade.min_stop_distance_ticks"
        ),
        max_spread_bps=D(_require(per_trade.get("max_spread_bps"), "per_trade.max_spread_bps")),
        max_slippage_bps=D(
            _require(per_trade.get("max_slippage_bps"), "per_trade.max_slippage_bps")
        ),
        max_daily_loss=_pct(drawdown.get("max_daily_loss_pct"), "drawdown.max_daily_loss_pct"),
        max_weekly_loss=_pct(drawdown.get("max_weekly_loss_pct"), "drawdown.max_weekly_loss_pct"),
        max_drawdown=_pct(drawdown.get("max_drawdown_pct"), "drawdown.max_drawdown_pct"),
        max_consecutive_losses=_positive_int(
            drawdown.get("max_consecutive_losses"), "drawdown.max_consecutive_losses"
        ),
        max_open_positions=_positive_int(
            concentration.get("max_open_positions"), "concentration.max_open_positions"
        ),
        max_exposure_per_asset=_exposure_pct(
            concentration.get("max_exposure_per_asset_pct"),
            "concentration.max_exposure_per_asset_pct",
        ),
        max_correlated_exposure=_exposure_pct(
            concentration.get("max_correlated_exposure_pct"),
            "concentration.max_correlated_exposure_pct",
        ),
        max_sector_exposure=_exposure_pct(
            concentration.get("max_sector_exposure_pct"), "concentration.max_sector_exposure_pct"
        ),
        max_market_exposure=market_exposure,
        market_overrides=overrides,
        event_blackout_minutes=int(events.get("blackout_minutes", 0) or 0),
        event_blackout_applies_above_timeframe_seconds=int(
            events.get("applies_to_timeframes_under_seconds", 3600) or 3600
        ),
        is_example=is_example,
    )


def _require(value: Any, name: str) -> Any:
    if value is None:
        raise RiskConfigError(f"risk limit {name!r} is required and has no default")
    return value


def load_limits(path: Path, *, allow_example: bool = False) -> RiskLimits:
    """Read a risk configuration file.

    Refuses the shipped example unless *allow_example* is set, so that a
    deployment cannot quietly run on numbers chosen by a stranger.
    """
    if not path.exists():
        raise RiskConfigError(
            f"no risk configuration at {path}. Copy config/risk.example.yaml to "
            f"{path}, read every number in it, and set your own."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise RiskConfigError(f"{path} must contain a YAML mapping")
    is_example = bool(raw.get("marker") == EXAMPLE_MARKER)
    if is_example and not allow_example:
        raise RiskConfigError(
            f"{path} is the shipped example file. It exists to show the format, "
            "not to be used. Remove the marker line once the numbers are yours."
        )
    return parse_limits(raw, is_example=is_example)
