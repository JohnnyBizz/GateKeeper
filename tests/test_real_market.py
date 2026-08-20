"""The engine, run against a market that actually happened.

Every other test here feeds the engine something built for the occasion. That
is right for testing behaviour and useless for testing judgement: a random
walk is unpredictable by construction, so a score that ranks setups perfectly
and one that ranks them by coin toss come out the same on it.

``data/recorded/`` holds real candles. What is asserted here is deliberately
*not* a win rate — that would be asserting the market, and a number from one
thirty-minute window would fail meaninglessly the moment anything else was
added. What is asserted is that the engine survives real data, that the walk
forward cannot see the future, and that the shape of the recording is intact.

That last one has teeth. This capture is the one that exposed the replay
dropping the platform's candle history, and the 1 MIN chart is the part that
was missing.
"""

from __future__ import annotations

import csv
import datetime
from pathlib import Path

import pytest

from poa.backtesting.paper import Backtester
from poa.chart_detection.csv_source import load_csv

RECORDED = Path(__file__).resolve().parent.parent / "data/recorded"
CAPTURE = RECORDED / "2026-08-19-pocketoption"
SECOND = RECORDED / "2026-08-20-eurusd"


def _timeframe(path: Path) -> int:
    return int(path.stem.rsplit("-", 1)[1].rstrip("s"))


class TestTheRecordingIsIntact:
    def test_the_capture_is_present(self):
        assert CAPTURE.is_dir(), "the recorded market is missing"
        assert list(CAPTURE.glob("*.csv"))

    def test_the_minute_chart_reaches_back_hours(self):
        """The half that the replay used to drop.

        Sub-minute charts come from live ticks and cover the recording. The
        minute chart comes from the platform's own history and reaches back
        far further — 193 candles from a thirty-minute capture.

        This guards the fixture, not the parser: the CSV is committed, so no
        code change can shrink it. What it stops is somebody regenerating this
        folder from a future recording, losing the history again without
        noticing, and leaving every measurement below quietly running on ticks
        alone. The parser itself is guarded in ``test_recording.py``.
        """
        series = load_csv(CAPTURE / "EUR-USD-OTC-60s.csv", timeframe_seconds=60)

        assert len(series) > 150
        span = series[-1].timestamp - series[0].timestamp
        assert span > datetime.timedelta(hours=3)

    def test_the_traded_pair_is_captured_going_both_ways(self):
        """One direction of drift flatters whichever way a tool leans.

        The pair that has to move both ways is the one being *traded*. Both
        captures also contain AUD/CAD, which streamed past on the same socket
        and was never on screen — and reading its 0.48% fall as "the recording
        was a falling market" is exactly the mistake this asserts against.
        On EUR/USD the two captures move in opposite directions, which is the
        thing that makes the pair of them worth having.
        """
        assert SECOND.is_dir(), "the second capture is missing"

        def drift(path: Path) -> float:
            series = load_csv(path, timeframe_seconds=60)
            return (series[-1].close - series[0].close) / series[0].close

        first = drift(CAPTURE / "EUR-USD-OTC-60s.csv")
        second = drift(SECOND / "EUR-USD-60s.csv")
        assert first > 0, "the first capture's traded pair should end higher"
        assert second < 0, "the second capture's traded pair should end lower"

    @pytest.mark.parametrize("folder", [CAPTURE, SECOND])
    def test_each_recording_says_which_chart_was_open(self, folder):
        """Without this the watchlist reads as the market being traded."""
        manifest = folder / "WHAT-IS-IN-HERE.txt"
        assert manifest.exists(), f"{folder.name} does not say what is in it"

        text = manifest.read_text(encoding="utf-8")
        assert "The chart that was open: EUR/USD" in text
        assert "AUD-CAD" in text and "not traded" in text

    @pytest.mark.parametrize(
        "path", sorted(CAPTURE.glob("*.csv")) + sorted(SECOND.glob("*.csv"))
    )
    def test_every_candle_is_a_candle(self, path):
        """Real data, so worth checking it is not quietly malformed."""
        with path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert rows

        last = None
        for row in rows:
            low, high = float(row["low"]), float(row["high"])
            open_, close = float(row["open"]), float(row["close"])
            assert low <= high
            assert low <= open_ <= high
            assert low <= close <= high
            stamp = datetime.datetime.fromisoformat(row["timestamp"])
            if last is not None:
                # Strictly forward, and never two candles in one bucket.
                assert stamp > last
            last = stamp

    @pytest.mark.parametrize(
        "path", sorted(CAPTURE.glob("*.csv")) + sorted(SECOND.glob("*.csv"))
    )
    def test_the_buckets_match_the_name_on_the_file(self, path):
        """A file called 60s holding 5s candles would silently mismeasure."""
        timeframe = _timeframe(path)
        with path.open(encoding="utf-8") as handle:
            stamps = [
                datetime.datetime.fromisoformat(row["timestamp"])
                for row in csv.DictReader(handle)
            ]
        gaps = {
            int((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:])
        }
        # Gaps are multiples of the timeframe: a quiet market skips buckets,
        # but it never produces one shorter than the bar.
        assert gaps, "a chart with one candle measures nothing"
        assert min(gaps) == timeframe
        assert all(gap % timeframe == 0 for gap in gaps)


class TestTheEngineSurvivesARealMarket:
    """No number here is a claim about the market. They are all about the run."""

    def _run(self, name: str, duration: int, window: int = 120):
        path = CAPTURE / name
        series = load_csv(path, timeframe_seconds=_timeframe(path))
        return Backtester(window=window, payout=0.92).run(
            series, trade_duration=duration, asset=path.stem, step=1,
            realistic_entry=True,
        )

    def test_it_walks_a_real_chart_without_falling_over(self):
        result = self._run("EUR-USD-OTC-60s.csv", 180)

        assert result.evaluated_bars > 50
        # Both halves present: a run that signals on every bar has no gates,
        # and one that signals on none has nothing to measure.
        assert result.trades
        assert result.wait_count

    def test_no_call_can_see_its_own_outcome(self):
        """Entry is the next bar's open, which is the first price on offer.

        Settling against the bar that produced the signal measures a trade
        nobody could place, and flatters it by exactly the amount price moved
        while the panel was being read.
        """
        path = CAPTURE / "EUR-USD-OTC-60s.csv"
        series = load_csv(path, timeframe_seconds=60)

        result = self._run("EUR-USD-OTC-60s.csv", 180)
        assert result.trades
        for trade in result.trades:
            # ``index`` is the bar that produced the signal, so entry belongs
            # to the one after it. Comparing against the signalling bar's
            # close would prove nothing either way: on a continuous market the
            # next open equals the last close most of the time, which is
            # exactly why this has to be checked by position and not by price.
            following = series[trade.index + 1]
            assert trade.entry_price == following.open

    def test_a_minute_chart_can_serve_the_expiries_a_second_chart_cannot(self):
        """The structural finding this capture produced.

        An expiry is judged against how long the current move is expected to
        stay valid, and that window is measured in *chart bars* — roughly
        ``2 + 10 x persistence`` of them. So the same five minutes is five
        bars on a 1 MIN chart and sixty on a 5 SEC chart, and only one of
        those fits inside a window that is rarely more than a dozen bars long.
        On this capture the duration fit came out 43.8 for 300s on the minute
        chart and 6.7 on the five-second one.

        Somebody reading a sub-minute chart and taking a five-minute trade is
        therefore acting on a number that was never about that trade. The fix
        is to read a chart whose bars match the expiry.

        Verified by mutation: widening the persistence window to swallow any
        horizon makes the 5s chart endorse 21 multi-minute expiries and this
        test fail. It is that window doing the work, not the separate cap on
        an expiry outliving its setup, which never binds here.
        """
        fast = sum(len(self._run("EUR-USD-OTC-5s.csv", d, window=120).trades)
                   for d in (180, 300))
        slow = sum(len(self._run("EUR-USD-OTC-60s.csv", d).trades)
                   for d in (180, 300))

        assert fast == 0, "a 5s chart should not be endorsing multi-minute expiries"
        assert slow > 0, "a 1m chart should be able to"
