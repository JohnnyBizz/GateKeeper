"""Render the finder's result as text a person can act on.

Order matters. Each pick leads with whether the setup has a measured edge,
because that is the single most important fact about it and the one most
easily skimmed past. Evidence against is printed in full, right after the
evidence for — not folded away — because the reasons not to take a trade
are the ones people least want to read.
"""

from __future__ import annotations

from gtcc.opportunities.evidence import FAMILY_LABEL, Stance
from gtcc.opportunities.finder import FinderResult, Pick
from gtcc.opportunities.setups import _px

MARK = {Stance.FOR: "+", Stance.AGAINST: "-", Stance.NEUTRAL: "~", Stance.UNKNOWN: "?"}


def _pick_lines(rank: int, pick: Pick) -> list[str]:
    c = pick.candidate
    dossier = pick.dossier
    lines = [
        f"#{rank}  {pick.headline}",
        f"    {pick.edge_label}",
        f"    Entry ~{c.entry}   Stop {c.stop}   Target {c.target}"
        + (f"   Reward:risk {pick.net_reward_risk:.2f}" if pick.net_reward_risk is not None else ""),
    ]
    if pick.approved_quantity is not None:
        lines.append(
            f"    Your risk engine would size this at {pick.approved_quantity} units"
            + (f", about {pick.risk_at_stop:,.2f} at risk" if pick.risk_at_stop is not None else "")
        )
    lines.append(f"    Chart: {pick.chart_url}")
    if not pick.exact_chart:
        lines.append(
            "    (TradingView may open a different exchange's prices for this "
            "symbol than the ones analysed — check the levels match.)"
        )
    lines.append(
        "    Entry is the last close when scanned. The market will have moved "
        "by the time you look; re-check the levels on the chart."
    )
    for title, stance in (
        ("FOR", Stance.FOR), ("AGAINST", Stance.AGAINST),
        ("NEUTRAL", Stance.NEUTRAL), ("NOT CHECKED", Stance.UNKNOWN),
    ):
        items = dossier.by_stance(stance)
        if not items:
            continue
        lines.append(f"    {title}:")
        for item in items:
            lines.append(f"      {MARK[stance]} {FAMILY_LABEL[item.family]}: {item.statement}")
    return lines


def render(result: FinderResult) -> list[str]:
    lines = [result.summary(), ""]
    if result.picks:
        lines.append(
            "Picks are ranked: a measured edge first, then by how many "
            "independent families of evidence agree."
        )
        lines.append("")
        for rank, pick in enumerate(result.picks, 1):
            lines += _pick_lines(rank, pick)
            lines.append("")
    if result.near_misses:
        lines.append("CLOSEST NEAR-MISSES — patterns that were present but did not clear the bar:")
        for miss in result.near_misses:
            c = miss.candidate
            lines.append(f"  {miss.symbol} {c.direction} {c.label}:")
            for reason in miss.reasons:
                lines.append(f"      {reason}")
        lines.append("")
    if result.unread:
        lines.append("NOT LOOKED AT — these are not findings about these symbols:")
        for item in result.unread:
            lines.append(f"  {item.symbol}: {item.reason}")
        lines.append("")
    lines.append(
        "Nothing here is a prediction or advice. A pick means a stated pattern "
        "is present with the evidence listed — not that it will work."
    )
    return lines
