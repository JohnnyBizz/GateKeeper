"""The whole record, pooled across every session this journal holds.

A session report judges one sitting, and one sitting is nearly always too
small to judge anything — the reports say so themselves, session after
session. What no report showed until now is the thing that grows: every
settled call this journal has ever recorded, pooled and split along the
axes that could actually condition this market — the expiry traded, the
pair, the score shown, the hour of day. Those splits are the standing open
questions in FINDINGS.md, and this section is where their answers accrete
one session at a time.

The pooling is stated honestly on the page: calls cluster inside sessions
and sessions inside days, so a column's *direction* is the readable part,
not its second decimal. And the same rule the rest of the report lives by
applies to every row: below a meaningful sample no rate is printed at all —
a row says how many calls it holds and that they are too few, because a
percentage over a handful is the easiest lie a report can tell.

Tool calls only. A manual row is the user's trade, not one of the tool's
reads, and mixing the two would grade the tool on decisions it did not
make.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..backtesting.stats import band_of, wilson_interval
from ..logging_setup import get_logger
from ..models import format_duration

log = get_logger(__name__)

#: The same bar every other rate in the report answers to.
MEANINGFUL = 20

#: The pair table would otherwise grow one row per instrument ever glanced
#: at; the most-called carry nearly all the information.
MAX_PAIR_ROWS = 8


@dataclass
class LedgerRow:
    label: str
    wins: int = 0
    losses: int = 0

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def rate(self) -> float | None:
        if self.settled == 0:
            return None
        return round(self.wins / self.settled * 100.0, 1)


def _in_era(row: dict[str, Any], policy: str | None) -> bool:
    """Whether a live row's direction is named under the rulebook ``policy``.

    'read' includes the legacy NULL rows — every row from before the
    column existed named the read's side; None means every era. The SQL
    twin is ``Journal._policy_clause``; this is the one Python home.
    """
    if policy is None:
        return True
    stamp = row.get("policy")
    if policy == "read":
        return stamp in (None, "read")
    return stamp == policy


def collect_ledger(
    journal: Any, source: str | None = None, policy: str | None = None
) -> dict[str, list[LedgerRow]] | None:
    """Pool every settled tool call in the journal, split four ways.

    ``policy`` names the rulebook era the live tables show — 'reversed'
    since the promotion, 'read' before it, None for everything at once.
    The race table carries every shadow whole whichever era is asked for.

    Returns None when the journal holds nothing settled (or cannot be
    read) — the report then simply carries no ledger section, rather than
    an empty frame pretending to be one.
    """
    try:
        rows = journal.all_settled_calls(source=source)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not read the journal for the ledger: %s", exc)
        return None
    if not rows:
        return None

    # The live strategy's record and the shadow experiments' records are
    # kept apart everywhere: the main tables describe what the panel
    # actually called, and the race table is where the shadows compete
    # with it on equal, labelled terms.
    live = [r for r in rows if not r.get("experiment")]
    shadows = [r for r in rows if r.get("experiment")]

    # The tables show one rulebook era at a time. A direction across the
    # 2026-08-25 flip means the opposite thing, so pooling both eras into
    # one table would let a pair that was "working" for the old rulebook
    # recommend exactly the wrong side of the new one. The race table is
    # unaffected — each shadow's meaning is fixed by its label — and the
    # duel picks its own challenger from every live row (see mirror_duel).
    era_live = [r for r in live if _in_era(r, policy)]

    overall = LedgerRow("live strategy")
    by_expiry: dict[int, LedgerRow] = {}
    by_band: dict[tuple[int, int], LedgerRow] = {}
    by_pair: dict[str, LedgerRow] = {}
    by_hour: dict[str, LedgerRow] = {}
    by_experiment: dict[str, LedgerRow] = {}

    for row in shadows:
        label = str(row.get("experiment") or "")
        bucket = by_experiment.setdefault(label, LedgerRow(label))
        bucket.wins += row["outcome"] == "win"
        bucket.losses += row["outcome"] != "win"

    for row in era_live:
        won = row["outcome"] == "win"

        def tally(bucket: LedgerRow) -> None:
            bucket.wins += won
            bucket.losses += not won

        tally(overall)

        duration = int(row["trade_duration"] or 0)
        tally(
            by_expiry.setdefault(
                duration, LedgerRow(format_duration(duration))
            )
        )

        shown = float(row["overall_confidence"] or 0.0)
        band = band_of(shown)
        if band is not None:
            tally(
                by_band.setdefault(band, LedgerRow(f"{band[0]}-{band[1]}"))
            )

        asset = str(row["asset"] or "")
        if asset:
            tally(by_pair.setdefault(asset, LedgerRow(asset)))

        stamp = str(row["timestamp"] or "")
        # ISO timestamps put the hour at a fixed offset; anything malformed
        # simply stays out of the hour table rather than inventing an hour.
        if len(stamp) >= 13 and stamp[11:13].isdigit():
            hour = stamp[11:13]
            tally(by_hour.setdefault(hour, LedgerRow(f"{hour}:00")))

    pairs = sorted(by_pair.values(), key=lambda r: -r.settled)[:MAX_PAIR_ROWS]

    return {
        "overall": [overall],
        "expiry": [by_expiry[k] for k in sorted(by_expiry)],
        "band": [by_band[k] for k in sorted(by_band)],
        "pair": pairs,
        "hour": [by_hour[k] for k in sorted(by_hour)],
        # The race: the live strategy against every shadow, on the same
        # charts over the same sessions. Rows keep their own labels so a
        # winner here is a named, repeatable configuration — not a vibe.
        "race": [overall]
        + sorted(by_experiment.values(), key=lambda r: -r.settled),
        "duel": mirror_duel(live, shadows),
    }


def mirror_duel(
    live: list[dict[str, Any]],
    shadows: list[dict[str, Any]],
    window_seconds: float = 90.0,
) -> dict[str, Any]:
    """The two policies against each other at the same moments.

    The race table can mislead on its own: pooled rates sample different
    moments. The duel pairs rows chart-by-chart, expiry-by-expiry, within
    a short window, and counts who won. Who duels whom depends on the era:

    * Since the promotion, the **pre-flip** shadow (the old rulebook)
      challenges the **mirror** (the promoted one) — the pre-registered
      demotion comparison, and a perfect pairing because both race at the
      same sweep cadence over the same charts.
    * On a journal from before the pre-flip shadow existed, the READ-era
      live rows challenge the mirror — the original promotion comparison.
      Only the read era: a mirror call is the reverse of the raw read, so a
      live row from the reversed era is the mirror's own side, and pairing
      the two would print a policy duelling itself.

    Timestamps are ISO strings in one timezone (the journal's own), so
    they compare as datetimes; rows that fail to parse simply stay out.
    """
    from datetime import datetime

    def _when(row: dict[str, Any]) -> Any:
        try:
            return datetime.fromisoformat(str(row.get("timestamp")))
        except (TypeError, ValueError):
            return None

    mirrors = [
        (when, row)
        for row in shadows
        if str(row.get("experiment")) == "mirror"
        and (when := _when(row)) is not None
    ]
    pre_flip = [
        row for row in shadows if str(row.get("experiment")) == "pre-flip"
    ]
    read_live = [r for r in live if _in_era(r, "read")]
    challenger_rows = pre_flip if pre_flip else read_live
    challenger = "pre-flip" if pre_flip else "live"

    pool: dict[tuple[str, int], list[tuple[Any, dict[str, Any]]]] = {}
    for row in challenger_rows:
        when = _when(row)
        if when is None:
            continue
        key = (str(row.get("asset")), int(row.get("trade_duration") or 0))
        pool.setdefault(key, []).append((when, row))

    # Maximum-cardinality matching, not a greedy walk. Any greedy order —
    # first-come or tightest-gap-first alike — can let one mirror take the
    # only live row another mirror could pair with while its own alternative
    # goes unused, undercounting the exact number the promotion decision
    # reads. Augmenting paths (Kuhn's algorithm) find every pair that can
    # exist; candidates are tried tightest gap first so, among maximum
    # matchings, near ones are preferred.
    adjacency: list[list[tuple[tuple[str, int], int]]] = []
    for when, mirror_row in mirrors:
        key = (
            str(mirror_row.get("asset")),
            int(mirror_row.get("trade_duration") or 0),
        )
        options: list[tuple[float, tuple[str, int], int]] = []
        for l_index, (live_when, _live_row) in enumerate(pool.get(key) or []):
            gap = abs((live_when - when).total_seconds())
            if gap <= window_seconds:
                options.append((gap, key, l_index))
        options.sort(key=lambda option: option[0])
        adjacency.append([(key, l_index) for _gap, key, l_index in options])

    live_match: dict[tuple[tuple[str, int], int], int] = {}

    def _assign(m_index: int, visited: set) -> bool:
        # Path length is bounded by the matching size — dozens, not
        # thousands — so recursion is comfortably within limits.
        for node in adjacency[m_index]:
            if node in visited:
                continue
            visited.add(node)
            holder = live_match.get(node)
            if holder is None or _assign(holder, visited):
                live_match[node] = m_index
                return True
        return False

    for m_index in range(len(mirrors)):
        if adjacency[m_index]:
            _assign(m_index, set())

    duel: dict[str, Any] = {
        "pairs": 0, "opposite": 0, "mirror_wins": 0, "live_wins": 0,
        "challenger": challenger,
    }
    for (key, l_index), m_index in live_match.items():
        mirror_row = mirrors[m_index][1]
        live_row = pool[key][l_index][1]
        duel["pairs"] += 1
        if str(live_row.get("direction")) != str(mirror_row.get("direction")):
            duel["opposite"] += 1
        if mirror_row.get("outcome") == "win":
            duel["mirror_wins"] += 1
        if live_row.get("outcome") == "win":
            duel["live_wins"] += 1
    return duel


#: How the record's cells are introduced when pulled out of their tables —
#: "14:00" alone reads as a time of day only inside the BY HOUR table.
_KIND_LABEL = {
    "expiry": "{} expiry",
    "band": "scored {}",
    "pair": "{}",
    "hour": "{} UTC",
}


def record_cells(
    ledger: dict[str, list[LedgerRow]] | None, breakeven: float
) -> tuple[list[tuple[str, LedgerRow]], list[tuple[str, LedgerRow]]]:
    """The record's standing verdicts: cells above break-even, and the worst.

    A cell is one row of the pooled tables — an expiry, a score band, a
    pair, an hour — with a meaningful sample. This is the honest form of
    "search for any positive calls at any percentage": any cell at all
    qualifies, but only with twenty-plus settled behind it, because a
    positive rate over a handful is how every fitted-then-failed threshold
    in FINDINGS.md got chosen.
    """
    if not ledger:
        return [], []
    scored: list[tuple[str, LedgerRow]] = []
    for kind in ("expiry", "band", "pair", "hour"):
        for row in ledger.get(kind) or []:
            if row.settled >= MEANINGFUL and row.rate is not None:
                scored.append((_KIND_LABEL[kind].format(row.label), row))
    above = sorted(
        (cell for cell in scored if (cell[1].rate or 0) >= breakeven),
        key=lambda cell: -(cell[1].rate or 0),
    )
    below = sorted(
        (cell for cell in scored if (cell[1].rate or 0) < breakeven),
        key=lambda cell: (cell[1].rate or 0),
    )
    return above, below


def record_highlights(
    ledger: dict[str, list[LedgerRow]] | None, breakeven: float
) -> list[str]:
    """The record's answer at the top of the section, not buried in tables.

    Every session now opens on this: what the pooled journal says is
    working, and what it refuses, before a single new call is made. It is
    the visible form of the tool refining itself — the same numbers the
    tables carry, asked the question the user actually has.
    """
    above, below = record_cells(ledger, breakeven)
    if not above and not below:
        return []

    def _cell(label: str, row: LedgerRow) -> str:
        return f"{label}  {row.rate:.1f}% over {row.settled}"

    lines = [f"WHAT THE RECORD SAYS   (break-even {breakeven:.1f}%)"]
    if above:
        cells = " · ".join(_cell(*cell) for cell in above[:3])
        lines.append(f"  Working    {cells}")
    else:
        lines.append(
            "  Working    nothing clears break-even at 20+ settled yet"
        )
    if below:
        cells = " · ".join(_cell(*cell) for cell in below[:3])
        lines.append(f"  Failing    {cells}")
    lines.append("")
    return lines


def record_summary(
    ledger: dict[str, list[LedgerRow]] | None, breakeven: float
) -> str:
    """One panel-sized line of the same answer, for the session's start."""
    above, below = record_cells(ledger, breakeven)
    if not above and not below:
        return ""
    if not above:
        return (
            f"Record at start: no cell above break-even ({breakeven:.1f}%) "
            "at 20+ settled — the race is hunting one."
        )
    label, row = above[0]
    more = f" and {len(above) - 1} more" if len(above) > 1 else ""
    # One decimal, like every rate in the ledger — and never rounded up to
    # a whole number the record did not earn.
    return (
        f"Record at start: best cell {label} at {row.rate:.1f}% over "
        f"{row.settled}{more}; break-even {breakeven:.1f}%."
    )


def _row_line(row: LedgerRow) -> str:
    count = f"{row.settled} call{'' if row.settled == 1 else 's'}"
    if row.settled < MEANINGFUL:
        return f"   {row.label:<14}{count:>12}   too few to read a rate from"
    return (
        f"   {row.label:<14}{count:>12}   "
        f"{row.rate:5.1f}%  ({row.wins}W/{row.losses}L)"
    )


def _race_line(row: LedgerRow) -> str:
    """A race row carries its interval: these rows are the ones a promotion
    decision reads, and a bare rate over forty calls invites exactly the
    overclaim the rest of the report exists to prevent. The interval is
    ``wilson_interval`` from the stats module — the same arithmetic as the
    session header, imported rather than re-implemented, so the two can
    never silently disagree."""
    line = _row_line(row)
    if row.settled >= MEANINGFUL:
        interval = wilson_interval(row.wins, row.settled)
        if interval is not None:
            line += f"  [{interval[0]:.1f}, {interval[1]:.1f}]"
    return line


def ledger_lines(ledger: dict[str, list[LedgerRow]]) -> list[str]:
    """Render the pooled record in the report's own voice."""
    total = ledger["overall"][0]
    lines = [
        "Every settled call this journal holds under the current rulebook,",
        "across every session — the tool's own calls only, settled by their",
        "charts' prices. (A direction across the 2026-08-25 flip means the",
        "opposite thing, so the other era's rows stay out of these tables;",
        "the race below keeps every shadow whole.) Calls cluster inside",
        "sessions and sessions inside days, so read the direction of a",
        "column, not its decimals. A row under "
        f"{MEANINGFUL} calls",
        "shows no rate: it is not hiding, it is too small to have one.",
        "",
    ]
    for title, key in (
        ("BY EXPIRY", "expiry"),
        ("BY SCORE SHOWN", "band"),
        ("BY PAIR (most called)", "pair"),
        ("BY HOUR (UTC)", "hour"),
    ):
        if not ledger.get(key):
            continue
        lines.append(title)
        lines.extend(_row_line(row) for row in ledger[key])
        lines.append("")

    race = ledger.get("race") or []
    if len(race) > 1:  # the live row alone is not a race
        lines += [
            "THE RACE — shadow strategies read the same charts under",
            "different rulebooks, on paper, alongside the live one. A",
            "winner here is a named configuration, not a vibe — and it",
            "still has to hold up out of sample before it flies the panel.",
            "",
        ]
        lines.extend(_race_line(row) for row in race)
        duel = ledger.get("duel") or {}
        if isinstance(duel, dict) and duel.get("pairs", 0) >= 5:
            # The number a promotion — or a demotion — actually reads: not
            # pool against pool, which sample different moments, but the
            # two policies paired at the same moments. Since the flip the
            # challenger is the pre-flip rulebook; before it, the live rows.
            rate = duel["mirror_wins"] / duel["pairs"] * 100.0
            versus = (
                "the pre-flip rulebook"
                if duel.get("challenger") == "pre-flip"
                else "the live call it reverses"
            )
            lines += [
                "",
                f"   HEAD TO HEAD — each mirror call against {versus},",
                "   same chart and expiry within 90 seconds:",
                f"   {duel['pairs']} pairs, direction opposite in "
                f"{duel['opposite']}; the mirror won {duel['mirror_wins']} "
                f"({rate:.1f}%), the other side {duel['live_wins']}.",
            ]
    else:
        # The closing summary line — only when the race table did not just
        # print the same row. With shadows in the journal the live strategy
        # leads the race table, and the 2026-08-24 13:41 report showed what
        # appending it again looks like: the same record twice, reading as
        # though two different things were being said.
        lines.append(_row_line(total).replace("   ", "", 1))
    return lines
