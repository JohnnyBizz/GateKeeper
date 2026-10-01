"""Render a scan as text.

Separate from the engine so that nothing about presentation can change
what was measured. The one rule this file enforces is that a column with
no measurement prints as a dash, never as a zero: a relative volume shown
as 0.00 is a claim about a dead market, and "we could not compute it" is
not that claim.
"""

from __future__ import annotations

from decimal import Decimal

from gtcc.scanner.engine import ScanResult, ScanRow, ScanStatus, SortKey

DASH = "   —"


def _num(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return DASH
    return f"{value:,.{places}f}"


def render(result: ScanResult, *, sort_by: SortKey = SortKey.SIGNAL) -> list[str]:
    lines = [result.summary(), ""]

    rows = result.ranked(sort_by)
    if rows:
        header = (
            f"{'SYMBOL':<12}{'PRICE':>12}{'CHG%':>9}{'ATR%':>8}"
            f"{'RVOL':>8}{'SPRD':>8}  {'TREND':<9}{'REGIME':<15}SIGNAL"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for row in rows:
            signal = row.signal
            lines.append(
                f"{row.symbol:<12}"
                f"{_num(row.price, 5 if row.price and row.price < 10 else 2):>12}"
                f"{_num(row.change_pct):>9}"
                f"{_num(row.atr_pct):>8}"
                f"{_num(row.relative_volume):>8}"
                f"{_num(row.spread_bps, 1):>8}  "
                f"{(str(row.trend) if row.trend else '—'):<9}"
                f"{(str(row.regime) if row.regime else 'unknown'):<15}"
                + (
                    f"{signal.decision} {signal.strategy} (conviction {signal.conviction})"
                    if signal is not None
                    else "-"
                )
            )
    else:
        lines.append("No symbol was analysed. The rows below say why.")

    unread = result.not_analysed
    if unread:
        lines += [
            "",
            "NOT ANALYSED — these are not findings about these symbols:",
        ]
        for row in unread:
            lines.append(f"  {row.symbol:<12}{row.status:<15}{row.detail}")

    silent = [row for row in rows if row.signal is None and row.silent_because]
    if silent:
        lines += ["", "Why the quiet symbols were quiet:"]
        for row in silent:
            for reason in row.silent_because:
                lines.append(f"  {row.symbol:<12}{reason}")

    if not result.rows:
        lines.append("Nothing was requested.")
    return lines
