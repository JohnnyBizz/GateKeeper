"""The tick archive: keeping the input the analysis used to throw away.

The feed delivers the market several times a second and everything downstream
reads only the candles those ticks become. The bucketing destroys how price
moved *inside* each bar — the one input FINDINGS has ranked highest-value and
untouched since the ledger shipped. These tests pin the archive's contract:
ticks land whole and ordered, writes are batched so the feed thread never
waits on a disk per tick, the file is bounded by retention, and a broken
archive degrades to a silent no-op rather than stalling the socket reader.
"""

from __future__ import annotations

import sqlite3

from poa.storage.ticks import TickArchive


def _rows(path):
    with sqlite3.connect(str(path)) as conn:
        return conn.execute(
            "SELECT symbol, at, price FROM ticks ORDER BY at"
        ).fetchall()


class _Tick:
    def __init__(self, symbol, timestamp, price):
        self.symbol = symbol
        self.timestamp = timestamp
        self.price = price


class TestTicksLandWhole:
    def test_what_goes_in_comes_back_in_order(self, tmp_path):
        archive = TickArchive(tmp_path / "ticks.db")
        archive.extend(
            [
                _Tick("EURUSD_otc", 100.0, 1.10),
                _Tick("GBPUSD_otc", 101.0, 1.30),
                _Tick("EURUSD_otc", 102.0, 1.11),
            ]
        )
        archive.close()
        assert _rows(tmp_path / "ticks.db") == [
            ("EURUSD_otc", 100.0, 1.10),
            ("GBPUSD_otc", 101.0, 1.30),
            ("EURUSD_otc", 102.0, 1.11),
        ]

    def test_the_count_is_honest_before_and_after_flushing(self, tmp_path):
        archive = TickArchive(tmp_path / "t.db", flush_rows=100)
        archive.add("EURUSD_otc", 1.0, 1.0)
        archive.add("EURUSD_otc", 2.0, 1.0)
        assert archive.archived == 2  # buffered ticks are still ticks kept
        archive.flush()
        assert archive.archived == 2
        archive.close()


class TestWritesAreBatched:
    """The feed thread reads the market; it must not pay a disk write per tick."""

    def test_below_both_thresholds_nothing_touches_disk(self, tmp_path):
        clock = [0.0]
        archive = TickArchive(
            tmp_path / "t.db", flush_rows=10, flush_seconds=60.0,
            clock=lambda: clock[0],
        )
        for step in range(9):
            archive.add("EURUSD_otc", float(step), 1.0)
        # Not even the database file exists yet: the connection is lazy too.
        assert not (tmp_path / "t.db").exists()
        archive.close()
        assert len(_rows(tmp_path / "t.db")) == 9  # close flushes the tail

    def test_enough_rows_force_a_write(self, tmp_path):
        archive = TickArchive(
            tmp_path / "t.db", flush_rows=5, flush_seconds=60.0,
        )
        for step in range(5):
            archive.add("EURUSD_otc", float(step), 1.0)
        assert len(_rows(tmp_path / "t.db")) == 5

    def test_a_quiet_stream_still_lands_by_time(self, tmp_path):
        clock = [0.0]
        archive = TickArchive(
            tmp_path / "t.db", flush_rows=1000, flush_seconds=5.0,
            clock=lambda: clock[0],
        )
        archive.add("EURUSD_otc", 1.0, 1.0)
        clock[0] = 6.0
        archive.add("EURUSD_otc", 2.0, 1.0)  # the next tick triggers the flush
        assert len(_rows(tmp_path / "t.db")) == 2


class TestTheFileIsBounded:
    def test_old_ticks_are_pruned_when_the_next_run_first_writes(self, tmp_path):
        # The prune runs when the archive first touches the database — in a
        # real session that is the first tick, which every session has. A
        # run that never sees a tick has nothing to prune for.
        import time

        path = tmp_path / "t.db"
        now = time.time()
        first = TickArchive(path, retention_days=14.0)
        first.add("EURUSD_otc", now - 30 * 86400, 1.0)  # a month old
        first.add("EURUSD_otc", now - 60.0, 1.1)  # a minute old
        first.close()
        assert len(_rows(path)) == 2  # nothing pruned mid-run...

        second = TickArchive(path, retention_days=14.0)
        second.add("EURUSD_otc", now, 1.2)
        second.close()
        kept = _rows(path)  # ...the next run's first write sweeps the month
        assert [row[2] for row in kept] == [1.1, 1.2]


class TestABrokenArchiveNeverBreaksTheFeed:
    def test_an_unwritable_path_degrades_to_a_no_op(self, tmp_path):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("a file where the parent directory should be")
        archive = TickArchive(
            blocker / "t.db", flush_rows=1
        )
        archive.add("EURUSD_otc", 1.0, 1.0)  # must not raise
        archive.extend([_Tick("EURUSD_otc", 2.0, 1.0)])
        archive.close()
        assert archive.available is False

    def test_garbage_values_are_dropped_not_raised(self, tmp_path):
        archive = TickArchive(tmp_path / "t.db", flush_rows=1)
        archive.add("EURUSD_otc", "not a time", "not a price")
        archive.add("EURUSD_otc", 1.0, 1.0)
        archive.close()
        assert len(_rows(tmp_path / "t.db")) == 1


class TestTheFeedFeedsTheArchive:
    """The wiring: every tick the builders bucket is archived whole."""

    def _source(self, archive):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999, tick_archive=archive)
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        return source

    def test_stream_ticks_reach_the_archive(self, tmp_path):
        archive = TickArchive(tmp_path / "t.db", flush_rows=1)
        source = self._source(archive)
        start = 1_786_662_000
        source._handle("updateStream", [["EURUSD_otc", start + 1, 1.10]])
        source._handle("updateStream", [["GBPUSD_otc", start + 2, 1.30]])
        archive.flush()
        assert [row[0] for row in _rows(tmp_path / "t.db")] == [
            "EURUSD_otc", "GBPUSD_otc",
        ]

    def test_stopping_the_source_closes_the_archive(self, tmp_path):
        archive = TickArchive(tmp_path / "t.db", flush_rows=1000)
        source = self._source(archive)
        source._handle("updateStream", [["EURUSD_otc", 1_786_662_001, 1.10]])
        source.stop()  # never started; must still flush and close the archive
        assert len(_rows(tmp_path / "t.db")) == 1

    def test_an_archive_that_blows_up_does_not_take_the_feed_with_it(self):
        class Grenade:
            def extend(self, ticks):
                raise RuntimeError("disk on fire")

            def close(self):
                raise RuntimeError("still on fire")

        source = self._source(Grenade())
        source._handle("updateStream", [["EURUSD_otc", 1_786_662_001, 1.10]])
        source.stop()
        assert source._builder.forming is not None  # the candles kept building

    def test_no_archive_is_a_fine_way_to_run(self):
        source = self._source(None)
        source._handle("updateStream", [["EURUSD_otc", 1_786_662_001, 1.10]])
        assert source._builder.forming is not None


class TestTheBuilderWiresItFromConfig:
    def test_the_feed_source_gets_an_archive_by_default(self, tmp_path, monkeypatch):
        from poa import chart_detection
        from poa.config import Config

        config = Config()
        config.set("capture.source", "feed")
        config.set("capture.auto_launch_browser", False)
        config.set("storage.tick_archive", str(tmp_path / "ticks.db"))

        source = chart_detection.build_source(config)
        assert source.tick_archive is not None
        assert source.tick_archive.retention_days == 14.0

    def test_an_empty_path_turns_it_off(self, tmp_path):
        from poa import chart_detection
        from poa.config import Config

        config = Config()
        config.set("capture.source", "feed")
        config.set("capture.auto_launch_browser", False)
        config.set("storage.tick_archive", "")

        source = chart_detection.build_source(config)
        assert source.tick_archive is None
