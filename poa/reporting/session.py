"""What the assistant decided this session, written down for review.

The panel is a live instrument: it shows what is true at the moment you look
at it, and forgets. That is the wrong shape for judging whether the thing is
any good. Judging it needs the whole session laid out at once — every call, in
order, with the score that produced it and how it actually settled — in a file
that outlives the process and can be read by someone who was not there.

One distinction runs through the whole document and is stated in it plainly:
**nothing here was bought.** GateKeeper places no trades and touches no
account. Every call it makes is followed to expiry against the platform's own
prices, and priced at the stake and payout that were configured at the time.
That produces a return figure, and that figure is notional — what the calls
would have returned had each one been taken. A report that let those be read
as executed trades would be worse than no report, so the wording never leaves
it ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..backtesting.stats import breakeven_rate, wilson_interval
from ..logging_setup import get_logger
from ..models import format_duration, utcnow

log = get_logger(__name__)

WIDTH = 74
RULE = "=" * WIDTH
THIN = "-" * WIDTH

# Below this many settled calls a win rate is a number about nothing. Said
# every time one is printed, because a rate over a handful of trades is the
# single easiest way to fool somebody reading a report in a hurry.
MEANINGFUL = 20


@dataclass
class SessionReport:
    """A session's decisions, ready to be written out."""

    started: datetime
    ended: datetime
    source: str = ""
    charts: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    manual: list[dict[str, Any]] = field(default_factory=list)
    stake: float = 0.0
    payout: float = 0.92
    tuning: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    measurement: str = ""
    lesson: str = ""

    # -- what the calls came to --------------------------------------------

    @property
    def settled(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("outcome") in ("win", "loss")]

    @property
    def wins(self) -> int:
        return sum(1 for c in self.calls if c.get("outcome") == "win")

    @property
    def losses(self) -> int:
        return sum(1 for c in self.calls if c.get("outcome") == "loss")

    @property
    def open(self) -> int:
        """Calls whose expiry had not elapsed when the session ended.

        Only those. This used to be everything that was not a win or a loss,
        which swept up three other things and told the reader each of them had
        not expired yet — of five ties in one session, made between 11:41 and
        12:20 on thirty-second expiries, all five were reported that way in a
        session that ran until 12:46. They had elapsed. They were refunds.

        The same mislabel was fixed once already, for voids, and survived here
        for everything else that is not a win or a loss.
        """
        return sum(1 for c in self.calls if _outcome_of(c) == "open")

    @property
    def flat(self) -> int:
        """Expired at the price they opened at.

        A binary that settles exactly where it started is a refund: neither
        side was right. Counting it either way would move the rate for no
        reason, so it stays out — but it is not an unfinished trade and saying
        so hides a real outcome behind a wrong explanation.
        """
        return sum(1 for c in self.calls if _outcome_of(c) == "flat")

    @property
    def undecided(self) -> int:
        """Elapsed, and nothing could say how they came out.

        A wrong data source, an incompatible price scale, an expiry the app
        slept through. The journal marks these rather than guessing, and the
        report should repeat that rather than calling them unfinished.
        """
        return sum(1 for c in self.calls if _outcome_of(c) in ("void", "unknown"))

    @property
    def win_rate(self) -> float | None:
        total = self.wins + self.losses
        return round(self.wins / total * 100.0, 1) if total else None

    @property
    def breakeven(self) -> float:
        return breakeven_rate(self.payout)

    @property
    def notional(self) -> float:
        """What the calls would have returned at the configured stake.

        Would have. Nothing here was placed — see the module docstring, and
        the header of every file this writes.
        """
        return round(self.wins * self.stake * self.payout - self.losses * self.stake, 2)

    def duration(self) -> str:
        seconds = max((self.ended - self.started).total_seconds(), 0)
        hours, remainder = divmod(int(seconds), 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _outcome_of(call: dict[str, Any]) -> str:
    """A call's outcome, with the absent one named rather than left blank."""
    return str(call.get("outcome") or "open").lower()


def _money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _clock(stamp: Any) -> str:
    if isinstance(stamp, datetime):
        return stamp.strftime("%H:%M")
    text = str(stamp or "")
    if not text:
        return "--:--"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return text[:5]


def _heading(title: str, note: str = "") -> list[str]:
    line = title if not note else f"{title}{note.rjust(WIDTH - len(title))}"
    return ["", line, THIN]


def build_report(report: SessionReport) -> str:
    """Render the session as plain text.

    Plain text on purpose: it opens anywhere, it diffs, it pastes into an
    email, and it cannot quietly stop rendering the way a format with a viewer
    can. A report nobody can open is not a report.
    """
    lines: list[str] = [
        RULE,
        "GATEKEEPER — SESSION REPORT".center(WIDTH),
        RULE,
        f"Session    {report.started:%Y-%m-%d %H:%M} → {report.ended:%H:%M} UTC"
        f"  ({report.duration()})",
    ]
    if report.source:
        lines.append(f"Read from  {report.source}")
    if report.charts:
        lines.append(f"Charts     {', '.join(report.charts)}")

    lines += _heading("WHAT THIS IS")
    lines += [
        "GateKeeper places no trades and touches no account. Every call below",
        "was made by the assistant and then followed to expiry against the",
        "platform's own prices.",
        "",
        "The money is notional. Outcomes are priced at the stake and payout",
        "configured at the time, so the return is what these calls WOULD have",
        "returned had each one been taken — not a record of trades placed. Any",
        "trade actually placed was placed by hand, and is listed separately.",
        "",
        "One call is one setup, start to finish. A setup that holds above the",
        "gate for minutes is listed once, not once per expiry window: it is a",
        "single read on the market and it wins or loses as one. Counting the",
        "re-arms instead turned one twenty-eight-minute session into fifty-",
        "seven calls covering about half that many moves, and every rate",
        "measured over them counted the long-lived setups several times.",
    ]

    lines += _calls_section(report)
    lines += _result_section(report)
    lines += _manual_section(report)
    lines += _learning_section(report)
    lines += _caveats_section(report)

    lines += ["", RULE, "Analysis only — not a trading recommendation.".center(WIDTH), RULE, ""]
    return "\n".join(lines)


# Roughly how often a directional call appears, per chart length, measured by
# walking the recordings in ``data/recorded`` at the shipped gates. Not a
# promise — a quiet market is quieter still — but the right order of magnitude,
# and enough to tell "nothing happened" apart from "nothing works".
CALLS_EVERY_MINUTES = {5: 3, 10: 6, 15: 9, 30: 15, 60: 120}


def _silence_note(report: SessionReport) -> list[str]:
    """Say whether an empty session was expected on the charts being read."""
    minutes = max((report.ended - report.started).total_seconds() / 60.0, 0.0)
    lengths = []
    for name in report.charts:
        for seconds, label in ((5, "5 SEC"), (10, "10 SEC"), (15, "15 SEC"),
                               (30, "30 SEC"), (60, "1 MIN")):
            if name.endswith(label):
                lengths.append(seconds)
                break
    if not lengths:
        return []

    fastest = min(lengths)
    every = CALLS_EVERY_MINUTES.get(fastest)
    if every is None:
        return []
    expected = minutes / every

    out = [
        f"This session ran {minutes:.0f} minutes. On the fastest chart it was",
        f"reading, a call appears roughly every {every} minutes, so about"
        f" {expected:.1f} were",
        "expected — measured by replaying real recordings at these gates.",
    ]
    if expected < 1.0:
        out += [
            "",
            "So an empty session here is the expected outcome, not a fault.",
        ]
        if fastest >= 60:
            out += [
                "A 1 MIN chart is the quiet one. If you want calls at a useful",
                "rate, open the 5 SEC chart on the platform — the tool calls",
                "from the chart you have OPEN, and on that one it speaks every",
                "two or three minutes.",
            ]
    else:
        out += [
            "",
            "That is fewer than expected. Worth looking at the gate audit.",
        ]
    return out


def _calls_section(report: SessionReport) -> list[str]:
    count = len(report.calls)
    lines = _heading("CALLS MADE", f"{count} call{'' if count == 1 else 's'}")
    if not count:
        lines += [
            "No setup passed the gates this session.",
            "",
            "Not the same as nothing happening: it means every setup the",
            "assistant looked at failed at least one confirmation it requires.",
            "The gate audit below says which, and whether refusing them cost",
            "anything.",
        ]
        # And the question that actually matters when a session comes back
        # empty: was that unusual, or is this chart one the tool rarely
        # speaks on at all?
        #
        # Ten consecutive reports read "0 calls" and every one of them was
        # taken as evidence the tool was broken. Measured on real recordings
        # it produces a call every two or three minutes on a 5 SEC chart and
        # every eighteen to two hundred minutes on a 1 MIN one. An hour on
        # the slow chart is *expected* to be silent, and a report that could
        # not say so sent everybody hunting a bug that was not there.
        lines += ["", *_silence_note(report)]
        return lines

    lines.append(
        f"{'TIME':<6}{'PAIR':<14}{'DIR':<6}{'SCORE':>6}"
        f"{'EXPIRY':>9}{'ENTRY':>11}{'RESULT':>10}"
    )
    for call in report.calls:
        outcome = _outcome_of(call).upper()
        if outcome == "OPEN":
            outcome = "UNSETTLED"
        entry = call.get("price")
        lines.append(
            f"{_clock(call.get('timestamp')):<6}"
            f"{str(call.get('asset', ''))[:13]:<14}"
            f"{str(call.get('direction', '')):<6}"
            f"{float(call.get('overall_confidence') or 0):>6.0f}"
            f"{format_duration(int(call.get('trade_duration') or 0)):>9}"
            f"{(f'{entry:.5f}' if isinstance(entry, (int, float)) else '--'):>11}"
            f"{outcome:>10}"
        )
    return lines


def _result_section(report: SessionReport) -> list[str]:
    rate = report.win_rate
    lines = _heading("HOW THEY SETTLED")
    lines.append(f"Settled                   {report.wins}W / {report.losses}L")
    if report.open:
        lines.append(
            f"Still open at close       {report.open}"
            "  (expiry had not elapsed; excluded from the rate)"
        )
    if report.flat:
        lines.append(
            f"Flat                      {report.flat}"
            "  (expired where they opened — a refund; excluded from the rate)"
        )
    if report.undecided:
        lines.append(
            f"Could not be settled      {report.undecided}"
            "  (expired, but nothing could decide them; excluded from the rate)"
        )
    if rate is None:
        lines.append("Win rate                  — nothing settled")
        return lines

    lines.append(f"Win rate                  {rate:.1f}%")
    interval = wilson_interval(report.wins, report.wins + report.losses)
    if interval is not None:
        low, high = interval
        # A rate without an interval reads as a finding. Over one session it
        # is usually consistent with a working tool and a losing one at the
        # same time, and saying so costs less than finding out with money.
        verdict = (
            "clears break-even" if low > report.breakeven
            else "below break-even" if high < report.breakeven
            else "straddles break-even — cannot tell them apart yet"
        )
        lines.append(f"95% interval              {low:.1f}% .. {high:.1f}%   {verdict}")
    lines.append(
        f"Break-even at {report.payout * 100:.0f}% payout   {report.breakeven:.1f}%"
        f"   ({'above' if rate >= report.breakeven else 'below'})"
    )

    # What the same calls would have paid with no opinion at all. Derived from
    # the calls themselves: a CALL that won means price rose and a PUT that
    # won means it fell, so which way the market went is already recorded.
    #
    # This is the number that says whether the analysis did anything. Over a
    # rising session a tool that mostly says BUY posts a healthy rate for a
    # reason with nothing to do with its reading of the chart, and only this
    # comparison shows it.
    ups = downs = 0
    for call in report.settled:
        rose = (str(call.get("direction")) == "CALL") == (call.get("outcome") == "win")
        if rose:
            ups += 1
        else:
            downs += 1
    decided = ups + downs
    if decided:
        for name, count in (("always BUY", ups), ("always SELL", downs)):
            base = count / decided * 100
            gap = rate - base
            note = (
                "the tool is ahead" if gap > 0
                else "level" if gap == 0
                else f"BEAT the tool by {-gap:.1f} points"
            )
            lines.append(f"  vs {name:<20}{base:.1f}%   {note}")

    lines.append(f"Notional stake            ${report.stake:,.2f} per call")
    lines.append(f"Notional return           {_money(report.notional)}  (not placed)")

    settled = report.wins + report.losses
    if settled < MEANINGFUL:
        lines += [
            "",
            f"{settled} settled call{'' if settled == 1 else 's'} is too few to read. A rate",
            f"needs around {MEANINGFUL} before it says anything about the next one;",
            "below that it is mostly noise, in either direction.",
        ]
    return lines


def _manual_section(report: SessionReport) -> list[str]:
    if not report.manual:
        return []
    lines = _heading("TRADES YOU PLACED", f"{len(report.manual)} recorded")
    lines += [
        "Entered by hand, or settled by the platform on the account being",
        "watched, and folded into the record the gates are tuned from.",
        "",
        "SCORE is what the assistant was calling that chart at the moment the",
        "trade opened. A dash means nothing of ours was live on it then, so",
        "there is no score to report and none is invented — the outcome still",
        "counts, but it teaches the score bands nothing.",
        "",
        f"{'TIME':<6}{'PAIR':<14}{'DIR':<6}{'SCORE':>6}{'RESULT':>10}",
    ]
    for trade in report.manual:
        # Zero is the marker for "nobody could attribute this", not a score of
        # nought. Printed as a number it reads as the assistant having rated
        # the trade zero out of a hundred and then been proved right or wrong
        # by it — which is a claim about the engine that nothing supports.
        score = float(trade.get("overall_confidence") or 0)
        lines.append(
            f"{_clock(trade.get('timestamp')):<6}"
            f"{str(trade.get('asset', ''))[:13]:<14}"
            f"{str(trade.get('direction', '')):<6}"
            f"{(f'{score:.0f}' if score > 0 else '—'):>6}"
            f"{str(trade.get('outcome') or '').upper():>10}"
        )
    return lines


def _learning_section(report: SessionReport) -> list[str]:
    lines: list[str] = []
    if report.measurement:
        lines += _heading("MEASURED ON THIS CHART")
        lines += _wrap(report.measurement)
    if report.tuning:
        lines += _heading("SETTINGS THE RECORD MOVED", f"{len(report.tuning)} change{'' if len(report.tuning) == 1 else 's'}")
        lines += [f"  {entry}" for entry in report.tuning]
    if report.retired:
        lines += _heading("RULES STOOD DOWN")
        lines += [
            "Measured to be refusing setups that would have paid, so they now",
            "warn instead of blocking:",
            "",
        ]
        lines += [f"  {entry}" for entry in report.retired]
    if report.lesson:
        lines += _heading("WHAT THE LOSSES HAD IN COMMON")
        lines += _wrap(report.lesson)
    return lines


def _caveats_section(report: SessionReport) -> list[str]:
    lines = _heading("READ THIS BEFORE THE NUMBERS")
    lines += [
        "· Nothing above was bought. The assistant has no access to the",
        "  account, places no orders, and never has.",
        "· The return is notional, at the stake and payout configured at the",
        "  time. A real fill differs: the price moves between the panel",
        "  lighting up and a button being pressed.",
        "· A win rate over a handful of calls is noise. Compare it to the",
        "  break-even rate for the payout, not to 50%.",
        "· Past settlement is not a forecast. No setup here was certain, and",
        "  the assistant does not claim any of them were.",
    ]
    return lines


def _wrap(text: str, width: int = WIDTH) -> list[str]:
    words, lines, current = str(text).split(), [], ""
    for word in words:
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def write_report(report: SessionReport, directory: str | Path) -> Path:
    """Write the session out, and return where it went.

    Named for when the session started rather than when it ended, so the files
    sort into the order they happened in and a reader can find the one they
    mean without opening any of them.
    """
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"session-{report.started:%Y-%m-%d-%H%M}.txt"
    path.write_text(build_report(report), encoding="utf-8")
    log.info("session report written to %s", path)
    return path


def collect(
    journal: Any,
    *,
    started: datetime,
    source: str | None = None,
    stake: float = 0.0,
    payout: float = 0.92,
    charts: Sequence[str] = (),
    tuning: Iterable[Any] = (),
    retired: Iterable[str] = (),
    measurement: str = "",
    lesson: str = "",
    ended: datetime | None = None,
) -> SessionReport:
    """Gather this session's rows out of the journal.

    Scoped by time and by source: rows from before the session belong to
    another report, and rows from a different data source describe a different
    experiment. A demo run must not turn up in a live one's numbers.
    """
    finished = ended or utcnow()
    calls: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []

    try:
        rows = journal.recent(limit=500)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not read the journal for the report: %s", exc)
        rows = []

    for row in rows:
        stamp = row.get("timestamp")
        when = None
        if isinstance(stamp, str) and stamp:
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                when = None
        if when is None or when < started:
            continue
        if source and row.get("source") not in (None, source):
            continue
        if row.get("direction") not in ("CALL", "PUT"):
            continue
        (manual if row.get("notes") == "manual" else calls).append(row)

    calls.reverse()  # journal reads newest first; a report reads forwards
    manual.reverse()

    return SessionReport(
        started=started,
        ended=finished,
        source=source or "",
        charts=list(charts),
        calls=calls,
        manual=manual,
        stake=float(stake),
        payout=float(payout),
        tuning=[str(getattr(t, "describe", lambda: t)()) for t in tuning],
        retired=[str(entry) for entry in retired],
        measurement=measurement,
        lesson=lesson,
    )
