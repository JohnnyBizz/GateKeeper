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
        "paired": same_moment_duels(shadows),
    }


def _pair_rows(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    window_seconds: float,
    same_expiry: bool,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair rows from two record sets at the same moments on the same chart.

    Maximum-cardinality matching, not a greedy walk. Any greedy order —
    first-come or tightest-gap-first alike — can let one row take the only
    partner another row could pair with while its own alternative goes
    unused, undercounting the exact number a promotion decision reads.
    Augmenting paths (Kuhn's algorithm) find every pair that can exist;
    candidates are tried tightest gap first so, among maximum matchings,
    near ones are preferred. Rows whose timestamps fail to parse stay out.
    """
    from datetime import datetime

    def _when(row: dict[str, Any]) -> Any:
        try:
            return datetime.fromisoformat(str(row.get("timestamp")))
        except (TypeError, ValueError):
            return None

    def _key(row: dict[str, Any]) -> tuple[Any, ...]:
        if same_expiry:
            return (str(row.get("asset")), int(row.get("trade_duration") or 0))
        return (str(row.get("asset")),)

    pool: dict[tuple[Any, ...], list[tuple[Any, dict[str, Any]]]] = {}
    for row in right:
        when = _when(row)
        if when is not None:
            pool.setdefault(_key(row), []).append((when, row))

    lefts = [(when, row) for row in left if (when := _when(row)) is not None]
    adjacency: list[list[tuple[tuple[Any, ...], int]]] = []
    for when, row in lefts:
        key = _key(row)
        options = []
        for index, (other_when, _other) in enumerate(pool.get(key) or []):
            gap = abs((other_when - when).total_seconds())
            if gap <= window_seconds:
                options.append((gap, key, index))
        options.sort(key=lambda option: option[0])
        adjacency.append([(key, index) for _gap, key, index in options])

    taken: dict[tuple[tuple[Any, ...], int], int] = {}

    def _assign(l_index: int, visited: set) -> bool:
        # Path length is bounded by the matching size — dozens, not
        # thousands — so recursion is comfortably within limits.
        for node in adjacency[l_index]:
            if node in visited:
                continue
            visited.add(node)
            holder = taken.get(node)
            if holder is None or _assign(holder, visited):
                taken[node] = l_index
                return True
        return False

    for l_index in range(len(lefts)):
        if adjacency[l_index]:
            _assign(l_index, set())

    return [
        (lefts[l_index][1], pool[key][index][1])
        for (key, index), l_index in taken.items()
    ]


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
    """
    mirrors = [r for r in shadows if str(r.get("experiment")) == "mirror"]
    pre_flip = [r for r in shadows if str(r.get("experiment")) == "pre-flip"]
    read_live = [r for r in live if _in_era(r, "read")]
    challenger_rows = pre_flip if pre_flip else read_live
    challenger = "pre-flip" if pre_flip else "live"

    duel: dict[str, Any] = {
        "pairs": 0, "opposite": 0, "mirror_wins": 0, "live_wins": 0,
        "challenger": challenger,
    }
    for mirror_row, live_row in _pair_rows(
        mirrors, challenger_rows, window_seconds=window_seconds, same_expiry=True
    ):
        duel["pairs"] += 1
        if str(live_row.get("direction")) != str(mirror_row.get("direction")):
            duel["opposite"] += 1
        if mirror_row.get("outcome") == "win":
            duel["mirror_wins"] += 1
        if live_row.get("outcome") == "win":
            duel["live_wins"] += 1
    return duel


#: The pre-registered same-moment comparisons, each a challenger against
#: the incumbent — the mirror, the promoted panel's own control. A
#: challenger marked reversed is scored on the OTHER side of its rows: a
#: settled call is a win or a loss, so a rulebook that loses has measured
#: exactly how its opposite would have done at the same moments.
#: (label, challenger experiment, challenger reversed, incumbent experiment)
SAME_MOMENT_DUELS: tuple[tuple[str, str, bool, str], ...] = (
    ("reversed three-minute", "three-minute", True, "mirror"),
    ("reversed strict-85", "strict-85", True, "mirror"),
    ("reversed three-minute-85", "three-minute-85", True, "mirror"),
    ("reversed five-minute", "five-minute", True, "mirror"),
    ("fade-overheat", "fade-overheat", False, "mirror"),
)


def same_moment_duels(
    shadows: list[dict[str, Any]], window_seconds: float = 20.0
) -> list[dict[str, Any]]:
    """Each pre-registered challenger against the mirror at the same reads.

    Every shadow is evaluated in the same sweep of the same chart, so two
    rulebooks' rows on one chart stamped within seconds of each other are
    the same read answered two ways — whatever expiry each settled at.
    That is the pairing a change of expiry or threshold has to win before
    it is recommended: pooled rates sample different moments and can lie.
    """
    by_label: dict[str, list[dict[str, Any]]] = {}
    for row in shadows:
        by_label.setdefault(str(row.get("experiment") or ""), []).append(row)

    results: list[dict[str, Any]] = []
    for label, challenger, reversed_, incumbent in SAME_MOMENT_DUELS:
        pairs = _pair_rows(
            by_label.get(challenger, []), by_label.get(incumbent, []),
            window_seconds=window_seconds, same_expiry=False,
        )
        if not pairs:
            continue
        wins = sum(
            1 for mine, _theirs in pairs
            if (mine.get("outcome") == "win") != reversed_
        )
        theirs = sum(1 for _mine, other in pairs if other.get("outcome") == "win")
        results.append({
            "label": label, "against": incumbent, "pairs": len(pairs),
            "wins": wins, "other_wins": theirs,
        })
    return results


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
        # The other side of the same rows. A settled call is a win or a
        # loss, so a rulebook's exact reverse is measured by the rulebook
        # itself — losses over settled — at the very same moments. Every
        # raw row on this board has sat below a coin flip since the race
        # began, which is the whole reason the mirror exists; and the
        # stricter the rulebook, the further below, which nobody had read
        # until the reverse was printed beside it.
        other = wilson_interval(row.losses, row.settled)
        if other is not None:
            line += (
                f"   reversed {100.0 - (row.rate or 0.0):.1f}% "
                f"[{other[0]:.1f}, {other[1]:.1f}]"
            )
    return line


def _paired_lines(paired: list[dict[str, Any]]) -> list[str]:
    """The pre-registered challengers against the mirror at the same reads."""
    shown = [duel for duel in paired if int(duel.get("pairs", 0)) >= 5]
    if not shown:
        return []
    lines = [
        "",
        "   SAME READS — each challenger against the mirror at the same",
        "   moment on the same chart, whichever expiry each settled at.",
        "   A challenger marked reversed is scored on the other side of",
        "   its own rows. The number a change of expiry or threshold has",
        "   to win before it is recommended:",
    ]
    for duel in shown:
        rate = duel["wins"] / duel["pairs"] * 100.0
        lines.append(
            f"   {duel['label']:<26}{duel['pairs']:>4} pairs   won "
            f"{duel['wins']} ({rate:.1f}%)   the mirror won {duel['other_wins']}"
        )
    return lines


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
            "Each row also shows its REVERSE: the same calls, the other",
            "side. A settled call is a win or a loss, so a rulebook that",
            "loses has measured exactly how its opposite would have done.",
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
        lines += _paired_lines(ledger.get("paired") or [])
    else:
        # The closing summary line — only when the race table did not just
        # print the same row. With shadows in the journal the live strategy
        # leads the race table, and the 2026-08-24 13:41 report showed what
        # appending it again looks like: the same record twice, reading as
        # though two different things were being said.
        lines.append(_row_line(total).replace("   ", "", 1))
    return lines
