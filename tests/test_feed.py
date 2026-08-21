"""Reading the platform's own data feed instead of a picture of it.

The recorder's job is to make an undocumented message format legible without
ever writing a credential to disk. Both halves are tested here; the browser
attachment itself needs a browser and is left to the thin layer that has one.
"""

from __future__ import annotations

import json

import pytest

from poa.feed.cdp import Target, pick_target
from poa.feed.frames import Summary, decode_frame
from poa.feed.redact import PLACEHOLDER, redact, redact_text


class TestRedaction:
    """A capture exists to be sent to someone. It must not carry a session."""

    def test_credential_shaped_keys_go_whatever_they_hold(self):
        cleaned = redact(
            {
                "authToken": "short",
                "user_session_id": 12,
                "password": "hunter2",
                "price": 0.95483,
            }
        )
        assert cleaned["authToken"] == PLACEHOLDER
        assert cleaned["user_session_id"] == PLACEHOLDER
        assert cleaned["password"] == PLACEHOLDER
        # The whole point is to keep the market data.
        assert cleaned["price"] == 0.95483

    def test_credential_shaped_values_go_whatever_key_they_arrived_under(self):
        cleaned = redact(
            {
                "harmless": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                "note": "ping user@example.com",
                "period": 30,
            }
        )
        assert cleaned["harmless"] == PLACEHOLDER
        assert "example.com" not in cleaned["note"]
        assert cleaned["period"] == 30

    def test_a_jwt_is_removed(self):
        text = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123"
        assert PLACEHOLDER in redact_text(text)

    def test_nested_structures_are_reached(self):
        cleaned = redact({"a": [{"b": {"token": "x"}}]})
        assert cleaned["a"][0]["b"]["token"] == PLACEHOLDER

    def test_candle_arrays_survive_intact(self):
        candles = [[1755102000, 0.95, 0.96, 0.94, 0.955]]
        assert redact({"data": candles})["data"] == candles


class TestFrameDecoding:
    def test_a_socket_io_event_is_named(self):
        frame = decode_frame('42["ticks",[["AUDCAD_otc",1755102000,0.95483]]]')
        assert frame.kind == "socket.io"
        assert frame.event == "ticks"
        assert frame.payload[1][0][2] == 0.95483

    def test_plain_json_is_decoded(self):
        frame = decode_frame('{"event":"candles","period":30}')
        assert frame.kind == "json"
        assert frame.event == "candles"

    def test_a_heartbeat_is_not_mistaken_for_data(self):
        """A bare "2" is a ping, and also valid JSON."""
        for payload, name in (("2", "ping"), ("3", "pong")):
            frame = decode_frame(payload)
            assert frame.kind == "socket.io"
            assert frame.event == name
            assert frame.payload is None

    def test_a_binary_frame_carrying_json_is_still_read(self):
        import base64

        raw = base64.b64encode(b'{"event":"tick","price":1.2}').decode()
        frame = decode_frame(raw, opcode=2)
        assert frame.kind == "json"
        assert frame.event == "tick"

    def test_genuinely_binary_frames_are_reported_not_guessed(self):
        import base64

        raw = base64.b64encode(bytes([0xFF, 0xFE, 0x00, 0x01])).decode()
        frame = decode_frame(raw, opcode=2)
        assert frame.kind == "binary"
        assert "not UTF-8" in frame.note

    def test_unparseable_text_is_truncated_and_redacted(self):
        frame = decode_frame("garbage " + "a1b2c3d4" * 8)
        assert frame.kind == "text"
        assert PLACEHOLDER in frame.payload

    def test_a_secret_never_survives_decoding(self):
        frame = decode_frame('42["auth",{"session":"abc","balance":3730.52}]')
        text = json.dumps(frame.payload)
        assert "abc" not in text
        assert "3730.52" not in text


class TestSummary:
    def _summary(self):
        summary = Summary()
        for payload in (
            '42["ticks",[["AUDCAD_otc",1,0.95]]]',
            '42["ticks",[["AUDCAD_otc",2,0.96]]]',
            '42["candles",{"period":30}]',
            "2",
        ):
            summary.add(decode_frame(payload))
        return summary

    def test_events_are_counted_and_ranked(self):
        summary = self._summary()
        assert summary.total == 4
        assert summary.by_event["ticks"] == 2
        rendered = summary.render()
        assert "ticks" in rendered
        assert rendered.index("ticks") < rendered.index("candles")

    def test_one_example_of_each_event_is_kept(self):
        summary = self._summary()
        assert "candles" in summary.samples
        assert summary.samples["ticks"][0] == "ticks"


class TestTargetSelection:
    def _targets(self):
        return [
            Target("1", "New Tab", "chrome://newtab", "ws://a"),
            Target("2", "Trading", "https://pocketoption.com/en/cabinet/", "ws://b"),
        ]

    def test_the_platform_tab_is_found_by_url(self):
        assert pick_target(self._targets()).websocket_url == "ws://b"

    def test_no_match_is_reported_rather_than_guessed(self):
        assert pick_target(self._targets(), needle="nowhere.example") is None

    def test_the_tab_with_the_chart_beats_another_tab_on_the_same_site(self):
        """A help page and a chart are both "the platform". Only one ticks."""
        targets = [
            Target("1", "Help", "https://pocketoption.com/en/help/", "ws://help"),
            Target("2", "Trading", "https://pocketoption.com/en/cabinet/", "ws://b"),
        ]
        assert pick_target(targets).websocket_url == "ws://b"

    def test_non_page_targets_are_ignored(self):
        assert Target.from_json({"type": "service_worker", "webSocketDebuggerUrl": "ws://x"}) is None
        assert Target.from_json({"type": "page", "id": "1"}) is None


class TestTheRealCaptureFormat:
    """Shapes taken verbatim from a live Pocket Option recording."""

    def _binary(self, text: str) -> str:
        import base64

        return base64.b64encode(text.encode()).decode()

    def test_a_binary_attachment_inherits_the_name_that_announced_it(self):
        """The name and the data arrive in two different frames.

        "451-" announces a BINARY_EVENT; its payload lands in the next frame.
        Read separately, 517 frames say "updateStream" with no data and 518 say
        nothing at all with all of it.
        """
        summary = Summary()
        summary.add(decode_frame('451-["updateStream",{"_placeholder":true,"num":0}]'))
        summary.add(
            decode_frame(self._binary('[["EURUSD_otc",1786662847.12,1.16233]]'), opcode=2)
        )
        assert summary.by_event["updateStream"] == 2
        assert summary.samples["updateStream"][0][0] == "EURUSD_otc"

    def test_unnamed_frames_are_grouped_by_shape_not_lumped_together(self):
        """A tick and a history block are both anonymous JSON arrays.

        Grouped under one "(json)" heading, whichever arrived first is the only
        one kept — and 518 ticks will always arrive before the history.
        """
        summary = Summary()
        for _ in range(3):
            summary.add(decode_frame('[["EURUSD_otc",1786662847.12,1.16233]]'))
        summary.add(
            decode_frame(
                '{"asset":"EURUSD_otc","period":30,"history":[[1786662000,1.161]]}'
            )
        )
        names = list(summary.by_event)
        assert len(names) == 2
        assert any("history" in name for name in names)


class TestTicks:
    def test_the_live_format_is_parsed(self):
        from poa.feed.ticks import parse_ticks

        ticks = parse_ticks([["EURUSD_otc", 1786662847.12, 1.16233]])
        assert len(ticks) == 1
        assert ticks[0].symbol == "EURUSD_otc"
        assert ticks[0].price == 1.16233

    def test_a_wrapped_event_is_parsed_too(self):
        from poa.feed.ticks import parse_ticks

        ticks = parse_ticks(["updateStream", [["AUDCAD_otc", 1.0, 0.95]]])
        assert ticks[0].symbol == "AUDCAD_otc"

    def test_rubbish_is_ignored_rather_than_guessed_at(self):
        from poa.feed.ticks import parse_ticks

        assert parse_ticks([]) == []
        assert parse_ticks("nonsense") == []
        assert parse_ticks([["EURUSD_otc"]]) == []
        assert parse_ticks([[1, 2, 3]]) == []

    def test_platform_symbols_become_readable_pairs(self):
        from poa.feed.ticks import display_symbol

        assert display_symbol("EURUSD_otc") == "EUR/USD OTC"
        assert display_symbol("AUDCAD_otc") == "AUD/CAD OTC"
        assert display_symbol("EURUSD") == "EUR/USD"


class TestCandleBuilder:
    def _ticks(self, prices, start=1_786_662_000, step=10):
        from poa.feed.ticks import Tick

        return [
            Tick("EURUSD_otc", start + i * step, price)
            for i, price in enumerate(prices)
        ]

    def test_ticks_become_ohlc_buckets(self):
        from poa.feed.ticks import CandleBuilder

        builder = CandleBuilder(period_seconds=30)
        # Three ticks in the first 30s bucket, then one in the next.
        builder.extend(self._ticks([1.10, 1.15, 1.12, 1.20]))

        settled = builder.settled
        assert len(settled) == 1
        assert settled[0].open == 1.10
        assert settled[0].high == 1.15
        assert settled[0].low == 1.10
        assert settled[0].close == 1.12

    def test_the_forming_candle_is_kept_apart_from_the_settled_ones(self):
        """The newest bar is always half-formed; calling it closed is a lie."""
        from poa.feed.ticks import CandleBuilder

        builder = CandleBuilder(period_seconds=30)
        builder.extend(self._ticks([1.10, 1.15, 1.12, 1.20]))
        assert builder.forming is not None
        assert builder.forming.close == 1.20
        assert builder.forming not in builder.settled

    def test_any_timeframe_can_be_built_from_the_same_ticks(self):
        from poa.feed.ticks import CandleBuilder

        prices = [1.0 + i * 0.01 for i in range(24)]  # 240 seconds of ticks
        for period, expected in ((30, 7), (60, 3), (120, 1)):
            builder = CandleBuilder(period_seconds=period)
            builder.extend(self._ticks(prices))
            assert len(builder.settled) == expected, period

    def test_a_series_carries_the_readable_symbol_and_period(self):
        from poa.feed.ticks import CandleBuilder

        builder = CandleBuilder(period_seconds=30)
        builder.extend(self._ticks([1.1, 1.2, 1.3, 1.4]))
        series = builder.series()
        assert series.symbol == "EUR/USD OTC"
        assert series.timeframe_seconds == 30

    def test_history_backfills_without_duplicating(self):
        from poa.feed.ticks import CandleBuilder

        builder = CandleBuilder(period_seconds=30)
        builder.extend(self._ticks([1.1, 1.2, 1.3, 1.4]))
        before = builder.settled
        builder.seed(before)  # the same candles again
        assert len(builder.settled) == len(before)

    def test_another_instrument_cannot_pollute_the_series(self):
        from poa.feed.ticks import CandleBuilder, Tick

        builder = CandleBuilder(period_seconds=30)
        builder.extend(self._ticks([1.10, 1.15]))
        builder.add(Tick("GBPJPY_otc", 1_786_662_005, 999.0))
        assert builder.forming.high == 1.15


class TestAnInstrumentThatGoesQuiet:
    """A tick-built bar only closes when the *next* tick arrives.

    So a pair that stops trading skipped every bucket it was silent for, and
    the series handed the indicators 22:44 and 23:25 as consecutive minutes —
    a forty-minute quiet spell read as one bar's move, on exactly the charts
    the watchlist builds from ticks alone.
    """

    def _quiet_for(self, minutes, period=60, run=40):
        from poa.feed.ticks import CandleBuilder, Tick

        builder = CandleBuilder(period_seconds=period, symbol="AUDCAD_otc")
        start = 1_786_662_000
        for step in range(run * period):
            builder.add(Tick("AUDCAD_otc", start + step, 1.10))
        resumes = start + run * period + minutes * period
        for step in range(10 * period):
            builder.add(Tick("AUDCAD_otc", resumes + step, 1.20))
        return builder.series()

    def _holes(self, series, period=60):
        stamps = [candle.timestamp for candle in series.candles]
        return [
            int((stamps[i + 1] - stamps[i]).total_seconds())
            for i in range(len(stamps) - 1)
            if (stamps[i + 1] - stamps[i]).total_seconds() != period
        ]

    def test_a_short_silence_is_drawn_flat(self):
        """No ticks means no trades, which means the price did not change —
        which is exactly what the platform draws."""
        series = self._quiet_for(3)
        assert self._holes(series) == []
        flat = [c for c in series.candles if c.open == c.high == c.low == c.close]
        assert len(flat) >= 3

    def test_a_filled_bar_carries_the_last_traded_price(self):
        series = self._quiet_for(3)
        stamps = {c.timestamp: c for c in series.candles}
        filled = [c for c in series.candles if c.volume == 0.0]
        assert filled and all(c.close == 1.10 for c in filled)

    def test_a_long_silence_is_left_as_a_gap(self):
        """Past a point it is not a quiet market but a closed one, and
        inventing that much calm is worse than admitting the break."""
        from poa.feed.ticks import MAX_FLAT_FILL_BUCKETS

        assert self._holes(self._quiet_for(MAX_FLAT_FILL_BUCKETS)) == []
        assert self._holes(self._quiet_for(MAX_FLAT_FILL_BUCKETS + 1))

    def test_a_gap_is_never_half_filled(self):
        """Fabricated bars *and* a hole would be the worst of both answers."""
        series = self._quiet_for(45)
        assert not [c for c in series.candles if c.volume == 0.0]

    def test_the_newest_bar_stops_being_hours_old(self):
        """Without this the panel reads a bar from an hour ago as the one
        currently forming, on any pair that has gone quiet."""
        from poa.feed.ticks import CandleBuilder, Tick

        builder = CandleBuilder(period_seconds=60, symbol="AUDCAD_otc")
        start = 1_786_662_000
        for step in range(600):
            builder.add(Tick("AUDCAD_otc", start + step, 1.10))
        assert builder.series().candles[-1].complete is False

        # The socket carries the whole market, so another pair ticking is
        # evidence that time has passed for this one too.
        builder.advance(start + 600 + 8 * 60)
        assert builder.series().candles[-1].complete is True

    def test_advancing_invents_nothing(self):
        """How long the silence will last is not known yet."""
        from poa.feed.ticks import CandleBuilder, Tick

        builder = CandleBuilder(period_seconds=60, symbol="AUDCAD_otc")
        start = 1_786_662_000
        for step in range(600):
            builder.add(Tick("AUDCAD_otc", start + step, 1.10))
        builder.advance(start + 600 + 40 * 60)
        assert not [c for c in builder.settled if c.volume == 0.0]

    def test_the_stream_closes_the_gap_after_an_advance(self):
        """Advancing settles the stale bar; the tick that ends the silence is
        what says how wide it was."""
        from poa.feed.ticks import CandleBuilder, Tick

        builder = CandleBuilder(period_seconds=60, symbol="AUDCAD_otc")
        start = 1_786_662_000
        for step in range(600):
            builder.add(Tick("AUDCAD_otc", start + step, 1.10))
        for step in range(0, 5 * 60, 30):
            builder.advance(start + 600 + step)
        builder.add(Tick("AUDCAD_otc", start + 600 + 5 * 60, 1.20))
        assert self._holes(builder.series()) == []

    def test_every_watched_chart_is_advanced_not_just_the_one_that_ticked(self):
        """Read off the builder rather than the watchlist, which separately
        declines to offer a chart that has fallen far behind."""
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        start = 1_786_662_000
        for step in range(180):
            source._handle("updateStream", [["EURUSD_otc", start + step, 1.10]])
            source._handle("updateStream", [["GBPUSD_otc", start + step, 1.30]])
        # Only one of them keeps trading, for two more bars.
        for step in range(180, 320):
            source._handle("updateStream", [["EURUSD_otc", start + step, 1.10]])

        builder = source._charts[("GBPUSD_otc", 60)]
        assert builder.settled[-1].complete is True
        assert builder.forming is None


class TestAChartThatHasStoppedTrading:
    """Dead charts kept their candles, and their candles kept scoring.

    The tab stayed lit, the setup stayed counted, and the panel went on
    offering a trade that had been available an hour earlier. Nothing upstream
    noticed, because the socket was busy — with everything else.
    """

    def _source(self, quiet_pair_stops_after=40):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._connected = True
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        start = 1_786_662_000
        for step in range(quiet_pair_stops_after * 60):
            source._handle("updateStream", [["EURUSD_otc", start + step, 1.10]])
            source._handle("updateStream", [["GBPUSD_otc", start + step, 1.30]])
        for step in range(quiet_pair_stops_after * 60, 130 * 60):
            source._handle("updateStream", [["EURUSD_otc", start + step, 1.10]])
        return source

    def test_a_dead_chart_is_not_offered(self):
        source = self._source()
        assert not [a for a, _t, _s in source.watched() if a == "GBP/USD OTC"]

    def test_the_live_charts_are_all_still_there(self):
        source = self._source()
        assert {t for a, t, _s in source.watched() if a == "EUR/USD OTC"} == {
            5, 10, 15, 30, 60
        }

    def test_a_whole_feed_stalling_singles_nobody_out(self):
        """The newest tick anywhere is the clock, so a stall puts every chart
        equally behind — and dropping all of them would be wrong."""
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        start = 1_786_662_000
        for step in range(40 * 60):
            source._handle("updateStream", [["EURUSD_otc", start + step, 1.10]])
            source._handle("updateStream", [["GBPUSD_otc", start + step, 1.30]])
        # Nothing has ticked since, for anyone.
        assert {a for a, _t, _s in source.watched()} == {"EUR/USD OTC", "GBP/USD OTC"}

    def test_the_open_chart_is_kept_even_when_it_is_the_quiet_one(self):
        """It is what the rest of the panel is about; capture says separately
        that it has gone quiet."""
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}]
        )
        assert ("GBP/USD OTC", 60) in {(a, t) for a, t, _s in source.watched()}

    def test_a_frozen_watched_chart_is_reported_too(self):
        """The open instrument's own clock says nothing about a watched one,
        so its silence is measured against the freshest candle anywhere."""
        import time

        source = self._source()
        source._last_message = time.monotonic()
        source._asset_seen = time.monotonic()

        assert source.focus("GBP/USD OTC", 60) is True
        quality = source.capture().quality
        assert quality.confidence == 25.0
        assert any("has not traded" in issue for issue in quality.issues)

    def test_a_live_watched_chart_is_not_accused_of_being_frozen(self):
        import time

        source = self._source()
        source._last_message = time.monotonic()
        source._asset_seen = time.monotonic()

        assert source.focus("EUR/USD OTC", 15) is True
        quality = source.capture().quality
        assert not any("has not traded" in issue for issue in quality.issues)

    def test_a_frozen_open_chart_is_reported(self):
        """The socket carries the whole market, so it stays busy while the
        pair on screen stops dead — and the panel reported LIVE FEED at full
        confidence over a chart that had not moved for an hour."""
        import time

        source = self._source()
        source._last_message = time.monotonic()

        source._asset_seen = time.monotonic() - 2
        assert source.capture().quality.confidence == 100.0

        source._asset_seen = time.monotonic() - 400
        quality = source.capture().quality
        assert quality.confidence == 25.0
        assert quality.ok is False
        assert any("has not traded" in issue for issue in quality.issues)


class TestTheFormingBarIsNotTreatedAsClosed:
    """The bar still building is not a shape yet, and must not be read as one.

    Ten seconds into a minute it is a doji; forty seconds later a full-bodied
    candle pointing the other way. A pattern layer reading it reports whichever
    it happened to be looked at, which is noise wearing the name of a signal.
    """

    def _ticks(self, prices, start=1_786_662_000, step=10):
        from poa.feed.ticks import Tick

        return [
            Tick("EURUSD_otc", start + i * step, price)
            for i, price in enumerate(prices)
        ]

    def test_the_forming_bar_is_marked_incomplete(self):
        from poa.feed.ticks import CandleBuilder

        builder = CandleBuilder(period_seconds=30)
        builder.extend(self._ticks([1.10, 1.15, 1.12, 1.20]))
        assert builder.forming is not None
        assert builder.forming.complete is False
        assert all(candle.complete for candle in builder.settled)

    def test_the_analysis_reads_only_closed_candles(self):
        """The forming bar reaches the price, and nothing else."""
        from datetime import datetime, timedelta, timezone

        from poa.analysis.timeframe import analyze_timeframe
        from poa.models import Candle, Series

        start = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        closed = [
            Candle(start + timedelta(minutes=i), 1.10, 1.11, 1.09, 1.10 + i * 0.001)
            for i in range(80)
        ]
        forming = Candle(
            start + timedelta(minutes=80), 1.18, 1.30, 1.05, 1.29, complete=False
        )

        analysis = analyze_timeframe(Series(closed + [forming], 60, "EUR/USD"))
        # The wild unfinished bar is not in what was analysed …
        assert len(analysis.series) == 80
        assert analysis.series.candles[-1] is closed[-1]
        # … but the price it is trading at is still the current one.
        assert analysis.price == 1.29

    def test_a_series_of_only_forming_candles_does_not_raise(self):
        from poa.analysis.timeframe import analyze_timeframe
        from poa.feed.ticks import CandleBuilder

        builder = CandleBuilder(period_seconds=60)
        builder.extend(self._ticks([1.10, 1.11]))
        analyze_timeframe(builder.series())  # must report, not explode

    def test_history_never_duplicates_the_bar_the_stream_is_building(self):
        """The platform's history ends with the bar that is still running.

        Taking it as well as the one the ticks are building puts two candles
        on one timestamp, which every indicator downstream silently averages.
        """
        from datetime import datetime, timezone

        from poa.feed.ticks import CandleBuilder
        from poa.models import Candle

        builder = CandleBuilder(period_seconds=60)
        builder.extend(self._ticks([1.10, 1.15], start=1_786_662_000, step=10))
        bucket = builder.forming.timestamp

        # History for the same bucket, as the platform would send it.
        builder.seed(
            [
                Candle(
                    datetime.fromtimestamp(1_786_661_940, tz=timezone.utc),
                    1.0, 1.1, 0.9, 1.05,
                ),
                Candle(bucket, 1.10, 1.99, 1.00, 1.99),
            ]
        )

        stamps = [candle.timestamp for candle in builder.series()]
        assert len(stamps) == len(set(stamps)), stamps
        # The stream owns the forming bar, so the history version loses.
        assert builder.forming.high == 1.15


class TestProtocol:
    """Message shapes copied verbatim from a live capture."""

    HISTORY = {
        "asset": "AUDUSD_otc",
        "index": 178666390886,
        "data": [
            {"symbol_id": 1, "time": 1786648980, "open": 0.70396, "close": 0.70362,
             "high": 0.70406, "low": 0.7036, "volume": 90},
            {"symbol_id": 1, "time": 1786649040, "open": 0.70362, "close": 0.70392,
             "high": 0.70392, "low": 0.70351, "volume": 105},
            {"symbol_id": 1, "time": 1786649100, "open": 0.70393, "close": 0.70402,
             "high": 0.7043, "low": 0.70386, "volume": 99},
            {"symbol_id": 1, "time": 1786649160, "open": 0.70404, "close": 0.70438,
             "high": 0.70444, "low": 0.70403, "volume": 96},
        ],
    }

    def test_history_becomes_real_candles_with_volume(self):
        from poa.feed.protocol import parse_history_candles

        asset, candles = parse_history_candles(self.HISTORY)
        assert asset == "AUDUSD_otc"
        assert len(candles) == 4
        assert candles[0].open == 0.70396
        assert candles[0].high == 0.70406
        assert candles[0].low == 0.7036
        assert candles[0].close == 0.70362
        # Volume is in the feed and was never available from the screen.
        assert candles[0].volume == 90

    def test_the_timeframe_can_be_inferred_from_history(self):
        from poa.feed.protocol import infer_period, parse_history_candles

        _asset, candles = parse_history_candles(self.HISTORY)
        assert infer_period(candles) == 60

    def test_a_symbol_change_names_the_instrument_and_the_timeframe(self):
        from poa.feed.protocol import parse_symbol_change

        change = parse_symbol_change(
            ["changeSymbol", {"asset": "AUDUSD_otc", "period": 60}]
        )
        assert change.asset == "AUDUSD_otc"
        assert change.period_seconds == 60
        assert change.display == "AUD/USD OTC"

    def test_tick_history_is_read(self):
        from poa.feed.protocol import parse_tick_history

        asset, period, ticks = parse_tick_history(
            {
                "asset": "AUDUSD_otc",
                "period": 60,
                "history": [[1786663113.601, 0.70069], [1786663114.101, 0.70066]],
            }
        )
        assert asset == "AUDUSD_otc"
        assert period == 60
        assert [tick.price for tick in ticks] == [0.70069, 0.70066]

    def test_unrecognised_shapes_yield_nothing_rather_than_guesses(self):
        """A renamed field must stop the feed, not invent candles."""
        from poa.feed.protocol import (
            parse_history_candles,
            parse_symbol_change,
            parse_tick_history,
        )

        assert parse_history_candles({"asset": "X", "data": "nope"}) == ("X", [])
        assert parse_history_candles({"data": [{"time": 1, "open": 2}]})[1] == []
        assert parse_symbol_change({"asset": "X"}) is None
        assert parse_symbol_change("nonsense") is None
        assert parse_tick_history({"history": [[1, 2]]})[2] == []

    def test_history_and_live_ticks_combine_into_one_series(self):
        from poa.feed.protocol import parse_history_candles, parse_tick_history
        from poa.feed.ticks import CandleBuilder

        _asset, candles = parse_history_candles(self.HISTORY)
        _a, _p, ticks = parse_tick_history(
            {
                "asset": "AUDUSD_otc",
                "period": 60,
                "history": [[1786649220.5, 0.70450], [1786649221.0, 0.70460]],
            }
        )
        builder = CandleBuilder(period_seconds=60, symbol="AUDUSD_otc")
        builder.seed(candles)
        builder.extend(ticks)

        series = builder.series()
        assert len(series) == 5  # four settled plus the one forming
        assert series.symbol == "AUD/USD OTC"
        assert series.timeframe_seconds == 60
        assert series.last_price == 0.70460


class TestWhichChartIsOpen:
    """The panel must name the instrument the platform is drawing.

    The socket carries ticks for many instruments at once, so "whichever
    symbol arrived first" is a coin toss — and it landed on the wrong pair,
    which made every number under it a report on a market the user was not
    looking at. Only a message where the page names its own chart counts.
    """

    def _source(self):
        from poa.feed.source import FeedChartSource

        return FeedChartSource(port=59999)

    def test_ticks_alone_never_choose_an_instrument(self):
        source = self._source()
        source.start = lambda: None
        source._connected = True  # the listener would have set this
        source._handle("updateStream", [["AUDCAD_otc", 1786663900.0, 0.9733]])
        source._handle("updateStream", [["CADJPY_otc", 1786663900.5, 115.3]])

        assert source._asset is None
        capture = source.capture()
        assert capture.asset is None
        assert capture.series is None
        # And it says what it is waiting for rather than showing a guess.
        assert any("which chart is open" in issue for issue in capture.quality.issues)

    def test_the_pages_own_chart_state_names_it(self):
        """``saveCharts`` travels outbound and carries the displayed symbol."""
        source = self._source()
        source._handle("updateStream", [["AUDCAD_otc", 1786663900.0, 0.9733]])
        source._handle(
            "saveCharts",
            ["saveCharts", {"settings": [{"symbol": "CADJPY_otc", "chartPeriod": 60}]}],
        )

        assert source._asset == "CADJPY_otc"
        assert source._period == 60

    def test_a_workspace_of_several_charts_is_not_guessed_at(self):
        source = self._source()
        source._handle(
            "saveCharts",
            [
                "saveCharts",
                {"settings": [{"symbol": "CADJPY_otc"}, {"symbol": "EURUSD_otc"}]},
            ],
        )
        assert source._asset is None

    def test_a_number_that_cannot_be_a_timeframe_is_not_used_as_one(self):
        """A wrong period silently rebuckets every candle in the series."""
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "CADJPY_otc", "period": 60}]
        )
        source._handle(
            "saveCharts",
            ["saveCharts", {"settings": [{"symbol": "CADJPY_otc", "chartPeriod": 3}]}],
        )
        assert source._period == 60

    def test_one_pair_at_several_lengths_is_several_charts(self):
        """The grid that took the panel off the chart being traded.

        EUR/USD at 1, 5 and 15 minutes in one workspace read as a single
        chart — instruments were counted, not charts — at whichever period
        the walk of the settings nest hit last. The claim carried the rank
        of the page naming its own chart, so every periodic save yanked the
        panel to the 15 MIN pane while the user traded the 1 MIN one.
        """
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        source._handle(
            "saveCharts",
            [
                "saveCharts",
                {"settings": [
                    {"symbol": "EURUSD_otc", "chartPeriod": 60},
                    {"symbol": "EURUSD_otc", "chartPeriod": 300},
                    {"symbol": "EURUSD_otc", "chartPeriod": 900},
                ]},
            ],
        )
        assert source._asset == "EURUSD_otc"
        assert source._period == 60, "the grid must not move the panel"

    def test_a_pinned_tab_survives_the_grid_being_saved(self):
        """The pin is the user's way of choosing the trading chart, and the
        grid's housekeeping saves were clearing it several times a minute."""
        from poa.feed.ticks import CandleBuilder

        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 900}]
        )
        source._charts[("EURUSD_otc", 60)] = CandleBuilder(
            period_seconds=60, symbol="EURUSD_otc"
        )
        assert source.focus("EURUSD_otc", 60)
        source._handle(
            "saveCharts",
            [
                "saveCharts",
                {"settings": [
                    {"symbol": "EURUSD_otc", "chartPeriod": 60},
                    {"symbol": "EURUSD_otc", "chartPeriod": 300},
                    {"symbol": "EURUSD_otc", "chartPeriod": 900},
                ]},
            ],
        )
        assert source._focus == ("EURUSD_otc", 60), "the pin is the user's"

    def test_one_pair_one_length_still_names_the_chart(self):
        """The single-chart case is the common one and must not be lost to
        the grid fix: one symbol at one period is still an answer."""
        from poa.feed.protocol import parse_displayed_chart

        asset, period = parse_displayed_chart(
            ["saveCharts", {"settings": [
                {"symbol": "CADJPY_otc", "chartPeriod": 300},
                {"symbol": "CADJPY_otc", "chartPeriod": 300},
            ]}]
        )
        assert (asset, period) == ("CADJPY_otc", 300)

    def test_the_grid_still_counts_as_workspace(self):
        """Giving up on "which chart is open" must not shrink "which charts
        does the user keep" — the watchlist filter reads the same nest."""
        from poa.feed.protocol import parse_workspace_charts

        charts = parse_workspace_charts(
            ["saveCharts", {"settings": [
                {"symbol": "EURUSD_otc", "chartPeriod": 60},
                {"symbol": "EURUSD_otc", "chartPeriod": 900},
            ]}]
        )
        assert charts == {"EURUSD_otc"}

    def test_a_history_request_names_the_chart_being_drawn(self):
        source = self._source()
        source._handle(
            "loadHistoryPeriod",
            ["loadHistoryPeriod", {"asset": "CADJPY_otc", "period": 30, "time": 1}],
        )
        assert source._asset == "CADJPY_otc"
        assert source._period == 30

    def test_history_arriving_first_is_enough_to_start(self):
        source = self._source()
        source._handle("loadHistoryPeriodFast", TestProtocol.HISTORY)
        assert source._asset == "AUDUSD_otc"
        assert source._period == 60  # inferred from the candle spacing
        assert len(source._builder.settled) == 4

    def test_a_weaker_claim_cannot_unseat_a_declared_chart(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "CADJPY_otc", "period": 60}]
        )
        source._handle("loadHistoryPeriodFast", TestProtocol.HISTORY)  # AUDUSD
        source._handle(
            "updateHistoryNewFast",
            {"asset": "EURUSD_otc", "period": 60, "history": [[1786663113.6, 1.16]]},
        )
        assert source._asset == "CADJPY_otc"
        assert source._builder.settled == []

    def test_an_equal_claim_moves_the_chart(self):
        """Two history messages in a row: the later one is the current chart."""
        source = self._source()
        source._handle("loadHistoryPeriodFast", TestProtocol.HISTORY)
        source._handle(
            "loadHistoryPeriod",
            ["loadHistoryPeriod", {"asset": "CADJPY_otc", "period": 60}],
        )
        assert source._asset == "CADJPY_otc"

    def test_a_silent_instrument_is_given_up(self):
        """If the chart we follow stops ticking, a weaker claim is believed.

        The platform does not always announce a switch in a message we rank
        highly. Holding on to a symbol that stopped streaming would leave the
        panel frozen on it for the rest of the session.
        """
        from poa.feed import source as module

        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "CADJPY_otc", "period": 60}]
        )
        source._asset_seen -= module.ASSET_STALE_SECONDS + 1
        source._handle("loadHistoryPeriodFast", TestProtocol.HISTORY)  # AUDUSD
        assert source._asset == "AUDUSD_otc"

    def test_a_timeframe_change_starts_a_new_chart(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        source._handle("updateStream", [["EURUSD_otc", 1786663900.0, 1.16]])
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 300}]
        )
        assert source._period == 300
        assert source._builder.forming is None  # 1m candles are not 5m candles

    def test_a_resync_forgets_the_chart_and_asks_again(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "AUDUSD_otc", "period": 60}]
        )
        source._handle("loadHistoryPeriodFast", TestProtocol.HISTORY)
        assert source._history_seen

        source.resync()
        assert source._asset is None
        assert source._history_seen is False
        assert source._refresh_requested is True

    def test_the_page_is_asked_to_reload_when_it_never_said_what_it_shows(self):
        """Attaching to a chart loaded minutes ago misses the whole bootstrap.

        Making the user switch timeframe to shake those messages loose is
        exactly the chore this source exists to remove.
        """
        import asyncio

        from poa.feed import source as module

        source = self._source()
        sent: list[Any] = []

        class FakeConnection:
            async def send(self, raw):
                sent.append(json.loads(raw))

        asyncio.run(
            source._maybe_refresh(
                FakeConnection(), attached_at=-module.BOOTSTRAP_GRACE_SECONDS * 2
            )
        )
        assert [message["method"] for message in sent] == ["Page.enable", "Page.reload"]

        # And not again a moment later — one reload, not a reload loop.
        sent.clear()
        asyncio.run(source._maybe_refresh(FakeConnection(), attached_at=-1000.0))
        assert sent == []

    def test_a_chart_that_is_reading_fine_is_left_alone(self):
        import asyncio

        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        source._history_seen = True
        sent: list[Any] = []

        class FakeConnection:
            async def send(self, raw):
                sent.append(json.loads(raw))

        asyncio.run(source._maybe_refresh(FakeConnection(), attached_at=-1000.0))
        assert sent == []

    def test_the_frames_that_name_the_chart_are_the_ones_the_page_sends(self):
        from poa.feed.frames import decode_frame

        frame = decode_frame(
            '42["changeSymbol",{"asset":"CADJPY_otc","period":60}]', direction="out"
        )
        source = self._source()
        source._handle(frame.event, frame.payload)
        assert source._asset == "CADJPY_otc"

    def test_an_unfamiliar_outbound_message_is_better_than_no_chart_at_all(self):
        """The protocol is undocumented and can be renamed under us."""
        source = self._source()
        source._handle(
            "someRenamedRequest",
            ["someRenamedRequest", {"asset": "CADJPY_otc", "period": 60}],
            "out",
        )
        assert source._asset == "CADJPY_otc"

        # And anything authoritative still replaces it.
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        assert source._asset == "EURUSD_otc"

    def test_the_listener_reads_what_the_page_sends_not_only_what_it_receives(
        self, monkeypatch
    ):
        """End to end through the DevTools loop, which is where this broke.

        Subscribing to received frames alone meant the messages that name the
        chart — all of them outbound — never arrived, and the panel was left
        following whichever instrument happened to tick first.
        """
        import asyncio

        from poa.feed import source as module

        source = self._source()
        target = Target(id="1", title="Pocket Option", url="https://x", websocket_url="ws://x")
        monkeypatch.setattr(module, "list_targets", lambda port, timeout=2.0: [target])
        monkeypatch.setattr(module, "pick_target", lambda targets, match: target)

        inbox = [
            {
                "method": "Network.webSocketFrameReceived",
                "params": {"response": {"opcode": 1, "payloadData":
                    '42["updateStream",[["AUDCAD_otc",1786663900.0,0.9733]]]'}},
            },
            {
                "method": "Network.webSocketFrameSent",
                "params": {"response": {"opcode": 1, "payloadData":
                    '42["changeSymbol",{"asset":"CADJPY_otc","period":60}]'}},
            },
            {
                "method": "Network.webSocketFrameReceived",
                "params": {"response": {"opcode": 1, "payloadData":
                    '42["updateStream",[["CADJPY_otc",1786663901.0,115.31]]]'}},
            },
        ]

        class FakeConnection:
            async def send(self, raw):
                return None

            async def recv(self):
                if inbox:
                    return json.dumps(inbox.pop(0))
                source._stop.set()
                raise asyncio.TimeoutError

        class FakeConnect:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return FakeConnection()

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            module, "websockets", type("W", (), {"connect": FakeConnect})
        )
        asyncio.run(source._listen())

        assert source._asset == "CADJPY_otc"
        assert source._period == 60
        assert source._builder.forming is not None
        assert source._builder.forming.close == 115.31


class TestFeedSource:
    def _source(self):
        from poa.feed.source import FeedChartSource

        return FeedChartSource(port=59999)

    def test_it_is_not_vision_based(self):
        """The whole point: nothing here can misread a chart."""
        source = self._source()
        assert source.vision_based is False
        assert source.name == "feed"

    def test_a_disconnected_feed_is_never_called_usable(self):
        """80 perfect candles from a socket that has dropped are still stale."""
        source = self._source()
        source.start = lambda: None
        source._handle("changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}])
        source._handle("updateStream", [["EURUSD_otc", 1786663900.0, 1.16]])
        assert not source.capture().quality.ok

    def test_before_any_message_it_says_so_rather_than_inventing_candles(self):
        source = self._source()
        source.start = lambda: None  # do not reach for a browser in a test
        capture = source.capture()
        assert capture.series is None
        assert not capture.quality.ok
        assert capture.quality.confidence == 0.0

    def test_a_symbol_change_starts_a_fresh_chart(self):
        source = self._source()
        source._handle("changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 30}])
        source._handle("updateStream", [["EURUSD_otc", 1786663900.0, 1.16]])
        assert source._asset == "EURUSD_otc"
        assert source._period == 30

        # Switching instrument must not carry the old candles across.
        source._handle("changeSymbol", ["changeSymbol", {"asset": "AUDCAD_otc", "period": 60}])
        assert source._builder.settled == []
        assert source._builder.forming is None

    def test_ticks_for_another_instrument_are_ignored(self):
        source = self._source()
        source._handle("changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}])
        source._handle("updateStream", [["EURUSD_otc", 1786663900.0, 1.16]])
        source._handle("updateStream", [["GBPJPY_otc", 1786663901.0, 999.0]])
        assert source._builder.forming.high == 1.16

    def test_history_for_a_pair_already_left_is_dropped(self):
        source = self._source()
        source._handle("changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}])
        source._handle("loadHistoryPeriodFast", TestProtocol.HISTORY)  # AUDUSD
        assert source._builder.settled == []

    def test_a_full_chart_reports_itself_usable(self):
        from poa.models import Candle
        from datetime import datetime, timedelta, timezone

        source = self._source()
        source.start = lambda: None
        source._connected = True  # the listener would have set this
        source._handle("changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}])

        start = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        source._builder.seed(
            [
                Candle(start + timedelta(minutes=i), 1.1, 1.2, 1.0, 1.15, 10.0)
                for i in range(80)
            ]
        )
        capture = source.capture()
        assert capture.asset == "EUR/USD OTC"
        assert capture.timeframe_seconds == 60
        assert len(capture.series) == 80
        assert capture.quality.ok

    def test_the_browser_is_started_rather_than_demanded(self, monkeypatch):
        """Opening GateKeeper should be the only thing the user has to do."""
        import asyncio

        from poa.feed import source as module

        feed = module.FeedChartSource(port=59997)
        calls: list[Any] = []

        def fake_list(port, timeout=2.0):
            if not calls:
                raise module.BrowserError("nothing listening")
            return []

        monkeypatch.setattr(module, "list_targets", fake_list)
        monkeypatch.setattr(
            module, "launch_browser", lambda profile, port=0: calls.append(port)
        )

        with pytest.raises(module.BrowserError):
            asyncio.run(feed._listen())  # no tab yet, but the browser started
        assert calls == [59997]

    def test_it_does_not_relaunch_the_browser_in_a_loop(self, monkeypatch):
        import asyncio

        from poa.feed import source as module

        feed = module.FeedChartSource(port=59996)
        launches: list[Any] = []
        monkeypatch.setattr(
            module,
            "list_targets",
            lambda port, timeout=2.0: (_ for _ in ()).throw(
                module.BrowserError("nothing listening")
            ),
        )
        monkeypatch.setattr(
            module, "launch_browser", lambda profile, port=0: launches.append(port)
        )

        for _ in range(3):
            with pytest.raises(module.BrowserError):
                asyncio.run(feed._listen())
        assert len(launches) == 1


class TestLookingAroundIsFree:
    """Switching charts to check something must not cost the history gathered.

    An hour of candles thrown away for a glance at another pair is a reason
    not to glance, and a tool that punishes looking is one that gets looked at
    less than it should be.
    """

    def _source(self):
        from poa.feed.source import FeedChartSource

        return FeedChartSource(port=59999)

    def _fill(self, source, symbol, prices, start=1_786_662_000):
        from poa.feed.ticks import Tick

        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": symbol, "period": 60}]
        )
        for i, price in enumerate(prices):
            source._builder.add(Tick(symbol, start + i * 30, price))

    def test_a_chart_keeps_its_candles_while_another_is_watched(self):
        source = self._source()
        self._fill(source, "EURUSD_otc", [1.10 + i * 0.001 for i in range(20)])
        held = len(source._builder.settled)
        assert held > 3

        # Go and look at something else …
        self._fill(source, "GBPJPY_otc", [190.0 + i * 0.01 for i in range(6)])
        assert source._asset == "GBPJPY_otc"
        assert len(source._builder.settled) < held

        # … and come back to find the work still there.
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        assert source._asset == "EURUSD_otc"
        assert len(source._builder.settled) == held

    def test_the_two_charts_never_mix(self):
        source = self._source()
        self._fill(source, "EURUSD_otc", [1.10 + i * 0.001 for i in range(20)])
        self._fill(source, "GBPJPY_otc", [190.0 + i * 0.01 for i in range(20)])
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for candle in source._builder.settled:
            assert candle.close < 10  # never a JPY price

    def test_the_same_pair_on_a_different_timeframe_is_a_different_chart(self):
        source = self._source()
        self._fill(source, "EURUSD_otc", [1.10 + i * 0.001 for i in range(20)])
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 300}]
        )
        assert source._builder.settled == []

    def test_remembering_is_bounded(self):
        from poa.feed.source import MAX_REMEMBERED_CHARTS

        source = self._source()
        for i in range(MAX_REMEMBERED_CHARTS + 5):
            self._fill(source, f"PAIR{i:02d}_otc", [1.1, 1.2, 1.3, 1.4])
        assert len(source._charts) <= MAX_REMEMBERED_CHARTS + 1


class TestTheWholeStreamIsUsed:
    """The socket carries every instrument whether or not one is being read.

    Reading one and discarding the rest meant a setup on another pair went
    unseen until the user happened to look — which is the job they were hoping
    to hand over.
    """

    def _source(self):
        from poa.feed.source import FeedChartSource

        return FeedChartSource(port=59999)

    def test_every_streaming_instrument_gets_its_own_candles(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for i in range(20):
            t = 1_786_662_000 + i * 30
            source._handle("updateStream", [["EURUSD_otc", t, 1.10 + i * 0.001]])
            source._handle("updateStream", [["GBPUSD_otc", t, 1.36 + i * 0.001]])
            source._handle("updateStream", [["USDJPY_otc", t, 157.0 + i * 0.01]])

        watched = {asset: series for asset, _tf, series in source.watched()}
        assert set(watched) == {"EUR/USD OTC", "GBP/USD OTC", "USD/JPY OTC"}
        for series in watched.values():
            assert len(series) > 3

    def test_the_open_chart_comes_first(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for i in range(6):
            t = 1_786_662_000 + i * 30
            source._handle("updateStream", [["GBPUSD_otc", t, 1.36]])
            source._handle("updateStream", [["EURUSD_otc", t, 1.10]])
        assert source.watched()[0][0] == "EUR/USD OTC"

    def test_prices_never_cross_between_instruments(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for i in range(20):
            t = 1_786_662_000 + i * 30
            source._handle("updateStream", [["EURUSD_otc", t, 1.10]])
            source._handle("updateStream", [["USDJPY_otc", t, 157.0]])
        for asset, _tf, series in source.watched():
            for candle in series:
                assert candle.close < 10 if asset == "EUR/USD OTC" else candle.close > 100

    def test_the_watchlist_uses_the_same_names_as_the_panel(self):
        """A watched chart and that chart once opened have to be one chart.

        The panel names the open chart ``EUR/USD OTC``. If the watchlist said
        ``EURUSD_otc`` the active tab would never light up, and the measurement
        kept for one would never be found for the other.
        """
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        source._handle("updateStream", [["GBPUSD_otc", 1_786_662_000, 1.36]])

        watched = source.watched()
        names = [asset for asset, _tf, _series in watched]
        # The open chart leads, and every name is in the panel's own form.
        assert names[0] == "EUR/USD OTC"
        assert set(names) == {"EUR/USD OTC", "GBP/USD OTC"}
        assert all(" OTC" in name for name in names)

        assert source.capture().asset == names[0]

    def test_a_chart_you_have_opened_outlives_one_that_just_ticked_past(self):
        """The platform ticks far more instruments than anyone trades.

        Without a preference the eight slots fill with whatever arrived first,
        and the pairs actually being worked on get pushed out by noise.
        """
        from poa.feed.source import MAX_REMEMBERED_CHARTS

        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        # A pair that was actually opened, then left.
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}]
        )
        assert ("EURUSD_otc", 60) in source._charts

        # Then a crowd of instruments that only ever ticked past.
        for i in range(MAX_REMEMBERED_CHARTS + 6):
            source._handle("updateStream", [[f"NOISE{i:02d}_otc", 1_786_662_000, 1.1]])
        # And another chart opened, forcing something out.
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "USDJPY_otc", "period": 60}]
        )

        assert ("EURUSD_otc", 60) in source._charts
        assert ("GBPUSD_otc", 60) in source._charts
        assert len(source._charts) <= MAX_REMEMBERED_CHARTS

    def test_the_watchlist_is_bounded(self):
        from poa.feed.source import MAX_REMEMBERED_CHARTS

        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for i in range(MAX_REMEMBERED_CHARTS + 10):
            source._handle(
                "updateStream", [[f"PAIR{i:02d}_otc", 1_786_662_000, 1.1]]
            )
        assert len(source._charts) <= MAX_REMEMBERED_CHARTS


class TestPickingAWatchedChart:
    """Reading another chart must not touch the platform.

    The stream already carries every instrument, so swapping between them is a
    choice about which candles to read. Nothing is clicked in the browser, no
    chart is opened, and no order is placed — the user's own rule, and the only
    way this can be safe to do from a panel.
    """

    def _source(self, *, extra=("GBPUSD_otc", "USDJPY_otc")):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for i in range(40):
            t = 1_786_662_000 + i * 20
            source._handle("updateStream", [["EURUSD_otc", t, 1.10 + i * 0.001]])
            for j, symbol in enumerate(extra):
                source._handle("updateStream", [[symbol, t, 100.0 + j + i * 0.01]])
        return source

    def test_a_watched_chart_can_be_read_instead(self):
        source = self._source()
        assert source.capture().asset == "EUR/USD OTC"

        assert source.focus("GBP/USD OTC") is True
        capture = source.capture()
        assert capture.asset == "GBP/USD OTC"
        assert capture.series is not None and len(capture.series) > 3
        # The candles are that chart's, not the one it replaced.
        assert all(c.close > 50 for c in capture.series)

    def test_the_raw_name_works_too(self):
        source = self._source()
        assert source.focus("GBPUSD_otc") is True
        assert source.capture().asset == "GBP/USD OTC"

    def test_picking_the_open_chart_goes_back_to_following_it(self):
        source = self._source()
        source.focus("GBP/USD OTC")
        assert source.focus("EUR/USD OTC") is True
        assert source._focus is None
        assert source.capture().asset == "EUR/USD OTC"

    def test_an_unknown_name_changes_nothing(self):
        source = self._source()
        assert source.focus("CADCHF OTC") is False
        assert source.capture().asset == "EUR/USD OTC"

    def test_opening_a_chart_on_the_platform_wins(self):
        """The browser is the user speaking too. Whichever they did last wins."""
        source = self._source()
        source.focus("GBP/USD OTC")
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "AUDCAD_otc", "period": 60}]
        )
        assert source._focus is None
        assert source.capture().asset == "AUD/CAD OTC"

    def test_nothing_is_asked_of_the_platform(self):
        """Focusing is a read.

        The only thing this class ever sends to the browser is a page reload,
        and that is driven by ``_refresh_requested``. Picking a chart must not
        set it, and must leave the chart the platform has open exactly as it
        was — the panel reads elsewhere, the browser is not steered.
        """
        source = self._source()
        before = (source._asset, source._period, source._builder)

        assert source.focus("GBP/USD OTC") is True

        assert source._refresh_requested is False
        assert (source._asset, source._period, source._builder) == before

    def test_a_watched_chart_says_it_has_no_platform_history(self):
        source = self._source()
        source.focus("USD/JPY OTC")
        capture = source.capture()
        assert capture.meta["feed"]["focused"] is True
        assert capture.meta["feed"]["history_loaded"] is False

    def test_a_forgotten_chart_releases_the_pick(self):
        """Focus must not outlive the candles behind it.

        Forgotten the way the app actually forgets, which drops the
        instrument's derived sub-minute charts along with it. Left behind,
        those would let the pick quietly survive by aggregating a stream that
        has stopped arriving — a chart still named on the panel, still scored,
        and frozen at whatever minute it was dropped.
        """
        source = self._source()
        source.focus("GBP/USD OTC")
        for key in [k for k in source._charts if k[0] == "GBPUSD_otc"]:
            del source._charts[key]
        source._forget_symbol_fast("GBPUSD_otc")

        assert source.capture().asset == "EUR/USD OTC"
        assert source._focus is None

    def test_a_pick_survives_while_the_pair_is_still_being_fed(self):
        """Dropping the minute chart does not blind the fifteen-second one."""
        source = self._source()
        assert source.focus("GBP/USD OTC", 15) is True
        for key in [k for k in source._charts if k[0] == "GBPUSD_otc"]:
            del source._charts[key]

        capture = source.capture()
        assert capture.asset == "GBP/USD OTC"
        assert capture.timeframe_seconds == 15


class TestATabOpensWhatItSays:
    """A chart is a pair *and* a length.

    ``focus`` took only a pair, so clicking the tab labelled ``EUR/USD 15SEC``
    handed back whichever length that pair happened to be followed at — the
    panel then read one minute under a label promising fifteen seconds. Every
    number on screen was right about a chart the user had not asked for.
    """

    def _source(self):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for i in range(600):
            t = 1_786_662_000 + i
            source._handle("updateStream", [["EURUSD_otc", t, 1.10 + i * 0.0001]])
            source._handle("updateStream", [["GBPUSD_otc", t, 100.0 + i * 0.001]])
        return source

    def test_every_tab_offered_can_be_opened(self):
        source = self._source()
        for asset, timeframe, _series in source.watched():
            assert source.focus(asset, timeframe) is not None
            capture = source.capture()
            assert (capture.asset, capture.timeframe_seconds) == (asset, timeframe)

    def test_a_sub_minute_tab_reads_that_length(self):
        source = self._source()
        assert source.focus("GBP/USD OTC", 15) is True
        capture = source.capture()
        assert capture.timeframe_seconds == 15
        # And they are that pair's candles, not the one it replaced.
        assert all(candle.close > 50 for candle in capture.series.candles)

    def test_a_sub_minute_tab_of_the_open_pair_is_not_a_no_op(self):
        """It resolved to the open chart and quietly did nothing at all."""
        source = self._source()
        assert source.focus("EUR/USD OTC", 5) is True
        assert source.capture().timeframe_seconds == 5

    def test_a_resampled_tab_opens_by_aggregating(self):
        """The watchlist offers timeframes nothing is held at directly."""
        source = self._source()
        assert source.focus("GBP/USD OTC", 300) is True
        capture = source.capture()
        assert capture.timeframe_seconds == 300
        assert capture.series is not None and len(capture.series) >= 1

    def test_a_length_that_cannot_be_built_is_refused(self):
        """Aggregation only works upwards, on whole multiples."""
        source = self._source()
        assert source.focus("GBP/USD OTC", 7) is False
        assert source.capture().asset == "EUR/USD OTC"

    def test_a_bare_pair_still_means_that_pair(self):
        """Typing a name into the box has no timeframe to offer."""
        source = self._source()
        assert source.focus("GBP/USD OTC") is True
        assert source.capture().asset == "GBP/USD OTC"


class TestTheWatchlistIsTheUsersOwnCharts:
    """The stream carries far more instruments than anyone trades.

    Left to itself the watchlist filled with whichever dozen ticked first —
    pairs the user has never looked at — while the tabs actually open on the
    platform never got in. The page's own ``saveCharts`` says which charts it
    keeps; that is the list that was being asked for.
    """

    def _source(self):
        from poa.feed.source import FeedChartSource

        return FeedChartSource(port=59999)

    def _workspace(self, *symbols):
        """A saveCharts payload shaped the way the platform nests them."""
        return [
            "saveCharts",
            {"charts": [{"symbol": s, "chartPeriod": 60} for s in symbols]},
        ]

    def test_only_the_charts_the_page_keeps_are_built(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}]
        )
        source._handle(
            "saveCharts",
            self._workspace("GBPUSD_otc", "EURUSD_otc", "USDJPY_otc", "LBPUSD_otc"),
        )
        for i in range(20):
            t = 1_786_662_000 + i * 30
            for symbol in ("GBPUSD_otc", "EURUSD_otc", "USDJPY_otc", "LBPUSD_otc"):
                source._handle("updateStream", [[symbol, t, 1.2]])
            # Instruments the user has never opened, ticking just as loudly.
            for symbol in ("GBPJPY_otc", "USDBRL_otc", "AUDCAD_otc"):
                source._handle("updateStream", [[symbol, t, 1.2]])

        watched = {asset for asset, _tf, _s in source.watched()}
        assert watched == {
            "GBP/USD OTC", "EUR/USD OTC", "USD/JPY OTC", "LBP/USD OTC"
        }

    def test_without_a_workspace_everything_on_the_stream_is_kept(self):
        """No saveCharts, no filter — better a noisy list than an empty one."""
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}]
        )
        source._handle("updateStream", [["GBPJPY_otc", 1_786_662_000, 1.2]])
        assert ("GBPJPY_otc", 60) in source._charts

    def test_a_nest_that_does_not_name_the_open_chart_is_not_trusted(self):
        """The guard against having parsed something else entirely.

        Whatever that nest was, it was not the list of charts the user has
        open — the one they are looking at would be on it. Filtering on it
        would leave the watchlist empty for a reason nobody could see.
        """
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}]
        )
        source._handle("saveCharts", self._workspace("XAUUSD", "BTCUSD"))
        source._handle("updateStream", [["GBPJPY_otc", 1_786_662_000, 1.2]])
        assert ("GBPJPY_otc", 60) in source._charts

    def test_closing_a_tab_does_not_throw_away_its_candles(self):
        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}]
        )
        source._handle("saveCharts", self._workspace("GBPUSD_otc", "EURUSD_otc"))
        for i in range(10):
            source._handle(
                "updateStream", [["EURUSD_otc", 1_786_662_000 + i * 30, 1.1]]
            )
        held = len(source._charts[("EURUSD_otc", 60)].settled)

        # The user closes the EUR/USD tab on the platform.
        source._handle("saveCharts", self._workspace("GBPUSD_otc"))
        for i in range(10):
            source._handle(
                "updateStream", [["EURUSD_otc", 1_786_662_600 + i * 30, 1.1]]
            )
        assert len(source._charts[("EURUSD_otc", 60)].settled) >= held

    def test_a_kept_chart_outlives_a_noisy_one(self):
        from poa.feed.source import MAX_REMEMBERED_CHARTS

        source = self._source()
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}]
        )
        # Noise first, so it is oldest and would go first anyway...
        for i in range(MAX_REMEMBERED_CHARTS):
            source._handle("updateStream", [[f"NOISE{i:02d}_otc", 1_786_662_000, 1.1]])
        # ...then the page says what it actually keeps.
        source._handle("saveCharts", self._workspace("GBPUSD_otc", "NOISE00_otc"))
        for _ in range(MAX_REMEMBERED_CHARTS):
            source._handle(
                "changeSymbol",
                ["changeSymbol", {"asset": "GBPUSD_otc", "period": 60}],
            )
        assert ("NOISE00_otc", 60) in source._charts

    def test_the_nest_is_walked_deep_enough_to_find_it(self):
        """Nobody designed saveCharts to be read; it is a settings blob.

        Stopping the walk short costs a workspace that is simply further down,
        and the only cost of going further is the walk itself.
        """
        from poa.feed.protocol import parse_workspace_charts

        nest = {"state": {"workspace": {"panes": {"left": {"tabs": [
            {"view": {"chart": {"symbol": "GBPUSD_otc", "chartPeriod": 60}}},
            {"view": {"chart": {"symbol": "EURUSD_otc", "chartPeriod": 60}}},
        ]}}}}}
        assert parse_workspace_charts(nest) == {"GBPUSD_otc", "EURUSD_otc"}

    def test_a_blob_with_no_instruments_in_it_filters_nothing(self):
        from poa.feed.protocol import parse_workspace_charts

        assert parse_workspace_charts({"layout": "grid", "theme": "dark"}) == set()


class TestTheSecondsCandlesNothingCouldSee:
    """Nothing can be aggregated downwards.

    Five-second candles cannot be recovered from one-minute ones, so a chart
    open at M1 had no way to see any of the platform's S5–S30 timeframes at
    all — while the ticks needed to build them were already arriving several
    times a second.
    """

    def _source(self, period=60, minutes=10, pairs=("EURUSD_otc",)):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": period}]
        )
        start = 1_786_662_000
        for i in range(int(minutes * 60 / 0.35)):
            when = start + i * 0.35
            for pair in pairs:
                source._handle("updateStream", [[pair, when, 1.19 + i * 0.00001]])
        return source

    def _lengths(self, source, asset="EUR/USD OTC"):
        return sorted(tf for a, tf, _s in source.watched() if a == asset)

    def test_a_one_minute_chart_now_offers_the_seconds_below_it(self):
        assert self._lengths(self._source()) == [5, 10, 15, 30, 60]

    def test_they_fill_far_faster_than_the_minute_chart(self):
        """Which is the point: S5 is readable in minutes, M1 takes an hour."""
        watched = {tf: s for _a, tf, s in self._source().watched()}
        assert len(watched[5]) > 100
        assert len(watched[5]) > len(watched[60]) * 5

    def test_nothing_is_built_at_or_above_the_open_chart(self):
        """Those come from aggregating candles already held.

        Building them twice would be two answers to one question, free to
        drift apart.
        """
        assert self._lengths(self._source(period=15)) == [5, 10, 15]

    def test_each_length_holds_only_its_own_prices(self):
        source = self._source(pairs=("EURUSD_otc",))
        for _a, tf, s in source.watched():
            for candle in s:
                assert 1.18 < candle.close < 1.25, tf

    def test_they_are_bounded(self):
        from poa.feed.source import MAX_FAST_CHARTS

        source = self._source(minutes=1)
        for i in range(12):
            source._handle("updateStream", [[f"NOISE{i:02d}_otc", 1_786_662_000, 1.1]])
        assert len(source._fast) <= MAX_FAST_CHARTS

    def test_only_the_charts_the_page_keeps(self):
        """The workspace filter applies here too, or the seconds candles
        would reintroduce every instrument it exists to exclude."""
        source = self._source(minutes=1)
        source._handle(
            "saveCharts",
            ["saveCharts", {"charts": [{"symbol": "EURUSD_otc", "chartPeriod": 60}]}],
        )
        source._handle("updateStream", [["GBPJPY_otc", 1_786_662_900, 1.5]])
        assert not any(asset == "GBPJPY_otc" for asset, _p in source._fast)


class TestReplayingARealRecording:
    """Generated markets cannot answer the question that matters.

    A random walk is unpredictable by construction: past its drift there is
    nothing in it to find, so a score that ranks setups perfectly and one that
    ranks them by coin toss measure the same on it. Recorded traffic is the
    only offline market that can say whether the reading works.
    """

    def _recording(self, tmp_path, ticks=4200, asset="EURUSD_otc", period=60):
        """Roughly twenty-five minutes of ticks — long enough that even the
        one-minute chart clears the floor the analysis needs."""
        import json

        path = tmp_path / "rec.jsonl"
        lines = [
            {"direction": "in", "kind": "socket.io", "event": "changeSymbol",
             "payload": ["changeSymbol", {"asset": asset, "period": period}]}
        ]
        start = 1_786_662_000
        for i in range(ticks):
            lines.append(
                {"direction": "in", "kind": "socket.io", "event": "updateStream",
                 "payload": [[asset, start + i * 0.35, 1.19 + (i % 200) * 0.00002]]}
            )
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def test_a_recording_becomes_candles(self, tmp_path):
        from poa.feed.replay import charts_from_recording

        charts = charts_from_recording(self._recording(tmp_path), min_candles=20)
        assert charts
        assert all(asset == "EUR/USD OTC" for asset, _tf, _s in charts)

    def test_every_timeframe_the_live_tool_would_have_had(self, tmp_path):
        """Through the live source's own handler, so the offline candles and
        the live ones cannot quietly drift apart."""
        from poa.feed.replay import charts_from_recording

        charts = charts_from_recording(self._recording(tmp_path), min_candles=20)
        assert sorted(tf for _a, tf, _s in charts) == [5, 10, 15, 30, 60]

    def test_the_prices_are_the_recorded_ones(self, tmp_path):
        from poa.feed.replay import series_from_recording

        series = series_from_recording(self._recording(tmp_path), min_candles=20)
        assert series is not None
        assert all(1.18 < candle.close < 1.20 for candle in series)

    def test_one_bad_line_does_not_lose_the_session(self, tmp_path):
        """A recording is a log, not a database."""
        from poa.feed.replay import charts_from_recording

        path = self._recording(tmp_path)
        path.write_text(path.read_text() + "not json at all\n{\n")
        assert charts_from_recording(path, min_candles=20)

    def test_an_empty_recording_yields_nothing_rather_than_failing(self, tmp_path):
        from poa.feed.replay import charts_from_recording, series_from_recording

        path = tmp_path / "empty.jsonl"
        path.write_text("")
        assert charts_from_recording(path) == []
        assert series_from_recording(path) is None

    def test_a_chart_can_be_asked_for_by_name_and_length(self, tmp_path):
        from poa.feed.replay import series_from_recording

        path = self._recording(tmp_path)
        series = series_from_recording(
            path, asset="EUR/USD OTC", timeframe=15, min_candles=20
        )
        assert series is not None and series.timeframe_seconds == 15
        assert series_from_recording(path, asset="NOT/HERE") is None

    def test_short_charts_are_left_out(self, tmp_path):
        """Below what the analysis reads there is nothing to measure."""
        from poa.feed.replay import charts_from_recording

        charts = charts_from_recording(self._recording(tmp_path), min_candles=200)
        assert all(len(s) >= 200 for _a, _tf, s in charts)


class TestRecordingLongEnoughToMatter:
    """A one-minute sample answers "what does the platform send".

    Capturing a real market to measure the engine against is a different job,
    and the recorder could not do it: it stopped at four thousand frames and
    held every one in memory, so "record for a few hours" ended after a few
    minutes. Frames now stream to disk as they arrive.
    """

    def _run(self, tmp_path, messages, seconds=5.0, max_frames=10**9):
        import asyncio
        import json
        import types

        from poa.feed import recorder

        class FakeConnection:
            def __init__(self, queued):
                self.queued = list(queued)

            async def send(self, _message):
                return None

            async def recv(self):
                if self.queued:
                    return self.queued.pop(0)
                await asyncio.sleep(10)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        original = recorder.websockets
        recorder.websockets = types.SimpleNamespace(
            connect=lambda *a, **k: FakeConnection(messages)
        )
        path = tmp_path / "out.jsonl"
        written = []
        try:
            with path.open("w", encoding="utf-8") as sink:
                def keep(frame):
                    sink.write(json.dumps(frame.to_dict(), default=str) + "\n")
                    written.append(1)

                capture = recorder.record(
                    types.SimpleNamespace(websocket_url="ws://x", url="http://x"),
                    seconds=seconds,
                    max_frames=max_frames,
                    on_frame=keep,
                )
                capture = asyncio.run(capture)
        finally:
            recorder.websockets = original
        return path, len(written), capture

    def _frames(self, count, with_symbol=True):
        import json

        def wrap(payload):
            return json.dumps(
                {"method": "Network.webSocketFrameReceived",
                 "params": {"response": {"opcode": 1, "payloadData": payload}}}
            )

        out = []
        if with_symbol:
            out.append(wrap('42["changeSymbol",{"asset":"EURUSD_otc","period":60}]'))
        for i in range(count):
            out.append(wrap(
                f'42["updateStream",[["EURUSD_otc",{1786662000 + i * 0.35},'
                f'{1.19 + (i % 300) * 0.00002}]]]'
            ))
        return out

    def test_a_dropped_connection_does_not_end_a_long_recording(self, tmp_path):
        """Over three hours a DevTools socket dropping at least once is close
        to certain — the page reloads, the network hiccups, the platform
        reconnects on its own. Stopping at the first of those turned "record
        for three hours" into "record until something twitches", and the user
        would only find out afterwards."""
        import asyncio
        import json
        import types

        from poa.feed import recorder

        batches = [self._frames(20), self._frames(20, with_symbol=False)]
        opened = []

        class Dropping:
            def __init__(self):
                self.queued = list(batches[len(opened)]) if len(opened) < len(batches) else []
                opened.append(1)
                self.first = len(opened) == 1

            async def send(self, _m):
                return None

            async def recv(self):
                if self.queued:
                    return self.queued.pop(0)
                if self.first:
                    # The socket dies mid-run, exactly once.
                    raise ConnectionResetError("devtools went away")
                await asyncio.sleep(10)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        original = recorder.websockets
        recorder.websockets = types.SimpleNamespace(connect=lambda *a, **k: Dropping())
        written = []
        try:
            with (tmp_path / "out.jsonl").open("w", encoding="utf-8") as sink:
                def keep(frame):
                    sink.write(json.dumps(frame.to_dict(), default=str) + "\n")
                    written.append(1)

                asyncio.run(recorder.record(
                    types.SimpleNamespace(websocket_url="ws://x", url="http://x"),
                    seconds=4.0, max_frames=10 ** 9, on_frame=keep,
                ))
        finally:
            recorder.websockets = original

        assert len(opened) > 1, "it never reconnected"
        # Everything from before the drop is still on disk, and the frames
        # from after it were captured too.
        assert len(written) > len(batches[0]), (
            f"only {len(written)} frames kept; the run stopped at the drop"
        )

    def test_it_records_past_the_old_ceiling(self, tmp_path):
        _path, written, _capture = self._run(tmp_path, self._frames(5000))
        assert written > 4000

    def test_nothing_accumulates_in_memory(self, tmp_path):
        """Which is what bounds a long recording by disk rather than by RAM."""
        _path, _written, capture = self._run(tmp_path, self._frames(5000))
        assert capture.frames == []

    def test_what_it_wrote_replays_into_candles(self, tmp_path):
        from poa.feed.replay import charts_from_recording

        path, _written, _capture = self._run(tmp_path, self._frames(6000))
        charts = charts_from_recording(path, min_candles=20)
        assert sorted(tf for _a, tf, _s in charts) == [5, 10, 15, 30, 60]

    def test_a_short_sample_still_collects_the_frames(self, tmp_path):
        """Without a sink the frames come back, which is all a sample needs."""
        import asyncio
        import types

        from poa.feed import recorder

        class FakeConnection:
            def __init__(self, queued):
                self.queued = list(queued)

            async def send(self, _m):
                return None

            async def recv(self):
                if self.queued:
                    return self.queued.pop(0)
                await asyncio.sleep(10)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        original = recorder.websockets
        recorder.websockets = types.SimpleNamespace(
            connect=lambda *a, **k: FakeConnection(self._frames(50))
        )
        try:
            capture = asyncio.run(
                recorder.record(
                    types.SimpleNamespace(websocket_url="ws://x", url="http://x"),
                    seconds=3.0,
                )
            )
        finally:
            recorder.websockets = original
        assert len(capture.frames) > 40

    def test_the_summary_is_still_built_while_streaming(self, tmp_path):
        """The readable report is what a protocol sample is *for*."""
        _path, _written, capture = self._run(tmp_path, self._frames(200))
        assert capture.summary.render()


class TestSendingTheDataBack:
    """Raw frames are large and mostly not market data.

    Every heartbeat and acknowledgement the platform sends for reasons of its
    own is in there, and none of it is what the engine gets measured against.
    The candles are, and the same recording as candles is small enough to
    actually send.
    """

    def _charts(self):
        from datetime import datetime, timedelta, timezone

        from poa.models import Candle, Series

        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        return [
            (
                "EUR/USD OTC",
                period,
                Series(
                    [
                        Candle(start + timedelta(seconds=i * period),
                               1.19, 1.1912, 1.1888, 1.1903)
                        for i in range(120)
                    ],
                    period,
                    "EUR/USD OTC",
                ),
            )
            for period in (5, 60)
        ]

    def test_each_chart_is_written(self, tmp_path):
        from poa.feed.replay import export_candles

        paths = export_candles(self._charts(), tmp_path)
        assert len(paths) == 2
        assert all(path.exists() for path in paths)

    def test_the_files_are_named_for_the_chart(self, tmp_path):
        from poa.feed.replay import export_candles

        names = sorted(p.name for p in export_candles(self._charts(), tmp_path))
        assert names == ["EUR-USD-OTC-5s.csv", "EUR-USD-OTC-60s.csv"]

    def test_they_load_back_through_the_reader_that_already_exists(self, tmp_path):
        """An exported chart is replayable by the tools there are, rather than
        by a new one written for the purpose."""
        from poa.chart_detection import load_csv
        from poa.feed.replay import export_candles

        path = sorted(export_candles(self._charts(), tmp_path))[0]
        series = load_csv(path)
        assert len(series) == 120
        assert series.timeframe_seconds == 5

    def test_the_prices_survive_the_round_trip(self, tmp_path):
        from poa.chart_detection import load_csv
        from poa.feed.replay import export_candles

        original = self._charts()[0][2]
        path = sorted(export_candles(self._charts(), tmp_path))[0]
        back = load_csv(path)
        assert back[0].open == original[0].open
        assert back[-1].close == original[-1].close

    def test_it_carries_prices_and_nothing_else(self, tmp_path):
        """Which is why the folder is safe to send without a second thought."""
        from poa.feed.replay import export_candles

        text = sorted(export_candles(self._charts(), tmp_path))[0].read_text()
        assert text.splitlines()[0] == "timestamp,open,high,low,close"

    def test_the_folder_is_created_if_it_is_not_there(self, tmp_path):
        from poa.feed.replay import export_candles

        assert export_candles(self._charts(), tmp_path / "a" / "b")

    def test_nothing_to_export_is_not_a_failure(self, tmp_path):
        from poa.feed.replay import export_candles

        assert export_candles([], tmp_path) == []


class TestWhatTheRealCaptureRevealed:
    """Three findings from a live recording of the platform.

    Every shape here is copied from traffic that was actually captured, not
    guessed at — which is the whole reason the recording was worth asking for.
    """

    # -- saveCharts describes ONE chart, not the tab list ------------------

    def _save(self, symbol):
        """A saveCharts exactly as the platform sends it."""
        return [
            "saveCharts",
            {"chartId": "x", "settings": {
                "chartId": "x", "chartType": 5, "chartPeriod": 4,
                "candlesTimer": True, "symbol": symbol, "fastTimeframe": 180}},
        ]

    def test_the_workspace_accumulates_across_messages(self):
        """One message, one chart. Replacing the set on each collapsed the
        watchlist to whichever chart the platform saved most recently — a
        worse answer than not filtering at all.
        """
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]
        )
        for symbol in ("EURUSD_otc", "GBPUSD_otc", "USDJPY_otc"):
            source._handle("saveCharts", self._save(symbol))
        assert source._workspace == {"EURUSD_otc", "GBPUSD_otc", "USDJPY_otc"}

    def test_the_captured_period_code_is_not_mistaken_for_seconds(self):
        """saveCharts reports chartPeriod 4 for a chart changeSymbol calls 60.

        It is a code, not a duration, and reading it as seconds would rebucket
        every candle on the chart.
        """
        from poa.feed.protocol import parse_displayed_chart

        _asset, period = parse_displayed_chart(self._save("EURUSD_otc"))
        assert period is None

    # -- updateAssets carries the payout -----------------------------------

    def _assets(self):
        return [
            [5, "#AAPL", "Apple", "stock", 2, 50, 60, 30, 3, 0, 170, 0, [],
             1786924800, False, [{"time": 60}], -1, 60, 1786984500],
            [170, "EURUSD_otc", "EUR/USD OTC", "currency", 3, 92, 60, 30, 3, 1,
             0, 5, [], 1786924800, True, [{"time": 60}], 0, 3, -1],
            [171, "LBPUSD_otc", "LBP/USD OTC", "currency", 3, 47, 60, 30, 3, 1,
             0, 5, [], 1786924800, True, [{"time": 60}], 0, 3, -1],
        ]

    def test_payouts_are_read_per_instrument(self):
        from poa.feed.protocol import parse_payouts

        payouts = parse_payouts(self._assets())
        assert payouts["EURUSD_otc"] == 0.92
        assert payouts["LBPUSD_otc"] == 0.47

    def test_the_payout_is_found_by_the_name_the_panel_uses(self):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle("updateAssets", self._assets())
        assert source.payout_for("EUR/USD OTC") == 0.92
        assert source.payout_for("LBP/USD OTC") == 0.47
        assert source.payout_for("NOT/HERE") is None

    def test_a_row_that_is_not_an_instrument_is_ignored(self):
        from poa.feed.protocol import parse_payouts

        assert parse_payouts([["junk"], None, 5, {}]) == {}

    def test_an_impossible_payout_is_not_believed(self):
        """Out of range means the column being read is not the payout."""
        from poa.feed.protocol import parse_payouts

        row = [1, "X_otc", "X", "currency", 3, 4000, 60, 30, 3, 1, 0, 5]
        assert parse_payouts([row]) == {}

    # -- successcloseOrder is a real settled trade -------------------------

    def _closed(self, **over):
        deal = {
            "asset": "USDJPY_otc", "openPrice": 158.611, "closePrice": 158.597,
            "command": 1, "profit": 8.8, "percentProfit": 88,
            "openTimestamp": 1786923484, "closeTimestamp": 1786923664,
        }
        deal.update(over)
        return {"profit": deal["profit"], "deals": [deal]}

    def test_a_settled_trade_is_read_whole(self):
        from poa.feed.protocol import parse_settled_trade

        trade = parse_settled_trade(self._closed())[0]
        assert trade["asset"] == "USDJPY_otc"
        assert trade["direction"] == "PUT"
        assert trade["won"] is True
        assert trade["duration"] == 180
        assert trade["payout"] == 0.88

    def test_both_ends_of_the_trade_are_kept(self):
        """The close is the only anchor for how far the platform's deal clock
        sits from this machine's, and without it a session's hand trades are
        matched against a clock that may be a timezone away."""
        from poa.feed.protocol import parse_settled_trade

        trade = parse_settled_trade(self._closed())[0]
        assert trade["opened_at"] == 1786923484
        assert trade["closed_at"] == 1786923664

    def test_a_losing_trade_reads_as_one(self):
        from poa.feed.protocol import parse_settled_trade

        trade = parse_settled_trade(
            self._closed(closePrice=158.70, profit=-10.0)
        )[0]
        assert trade["won"] is False

    def test_a_reading_that_contradicts_itself_is_discarded(self):
        """Direction, price move and outcome have to agree.

        Where they do not, one of them is being read wrongly, and a record
        taught backwards is worse than one not taught at all.
        """
        from poa.feed.protocol import parse_settled_trade

        # A put whose price rose, reported as a win.
        assert parse_settled_trade(
            self._closed(closePrice=159.00, profit=8.8)
        ) == []

    def test_a_trade_still_open_is_not_settled(self):
        from poa.feed.protocol import parse_settled_trade

        assert parse_settled_trade(self._closed(closePrice=0)) == []

    def test_settled_trades_are_handed_over_once(self):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        source._handle("successcloseOrder", self._closed())
        assert len(source.take_settled()) == 1
        assert source.take_settled() == []

    def test_the_queue_is_bounded(self):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        for _ in range(400):
            source._handle("successcloseOrder", self._closed())
        assert len(source._settled) <= 200


class TestAnUnnamedChartMakesTheRecorderAskForOne:
    """A real capture came back with 15,800 frames and zero candles.

    Prices arrive for the whole market whether or not anything has said which
    chart is being followed, so the run looks busy the entire time and builds
    nothing. The page announces its chart when it loads — and the chart is
    opened long before anybody thinks to record it, so by then those messages
    are already in the past.

    The live source has always asked the page to reload when it has not been
    told what it is showing. The recorder had no such thing, which is why half
    an hour of a falling market produced an empty file.
    """

    def _run(self, messages, seconds=1.0):
        import asyncio
        import types

        from poa.feed import recorder

        sent: list[dict] = []

        class FakeConnection:
            def __init__(self, queued):
                self.queued = list(queued)

            async def send(self, message):
                import json as _json
                sent.append(_json.loads(message))

            async def recv(self):
                if self.queued:
                    return self.queued.pop(0)
                await asyncio.sleep(10)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        original = recorder.websockets
        grace = recorder.NAMING_GRACE_SECONDS
        recorder.websockets = types.SimpleNamespace(
            connect=lambda *a, **k: FakeConnection(messages)
        )
        # The real grace is eight seconds; nobody should wait that long here.
        recorder.NAMING_GRACE_SECONDS = 0.0
        try:
            capture = asyncio.run(recorder.record(
                types.SimpleNamespace(websocket_url="ws://x", url="http://x"),
                seconds=seconds, max_frames=10**9,
            ))
        finally:
            recorder.websockets = original
            recorder.NAMING_GRACE_SECONDS = grace
        return capture, sent

    def _tick(self):
        import json
        return json.dumps({
            "method": "Network.webSocketFrameReceived",
            "params": {"response": {
                "payloadData": '42["updateStream",[["EURUSD_otc",1786663900.0,1.16]]]',
                "opcode": 1,
            }},
        })

    def _named(self):
        import json
        return json.dumps({
            "method": "Network.webSocketFrameSent",
            "params": {"response": {
                "payloadData": '42["changeSymbol",{"asset":"EURUSD_otc","period":60}]',
                "opcode": 1,
            }},
        })

    def test_anonymous_prices_make_it_ask_the_page_to_reload(self):
        capture, sent = self._run([self._tick()] * 6)

        methods = [m.get("method") for m in sent]
        assert "Page.reload" in methods, "the recorder never asked for a reload"
        assert not capture.named_a_chart

    def test_a_page_that_named_its_chart_is_left_alone(self):
        """Reloading a working capture would throw away what it had."""
        capture, sent = self._run([self._named()] + [self._tick()] * 6)

        assert "Page.reload" not in [m.get("method") for m in sent]
        assert capture.named_a_chart

    def test_it_asks_a_bounded_number_of_times(self):
        """A page that says nothing after two reloads will not start on the
        third, and a tab reloading itself forever is its own failure."""
        from poa.feed import recorder

        capture, sent = self._run([self._tick()] * 400, seconds=2.0)
        reloads = sum(1 for m in sent if m.get("method") == "Page.reload")

        assert 0 < reloads <= recorder.MAX_RELOADS

    def test_network_is_still_enabled_first(self):
        """The reload is an addition, not a replacement."""
        _capture, sent = self._run([self._tick()] * 4)

        assert sent[0].get("method") == "Network.enable"


class TestHistoryForTheTabsBehind:
    """The platform loads history for every tab it draws, not only the front
    one, and all of it crosses this socket exactly once.

    It was being dropped. A watched pair therefore began at zero candles and
    grew one per bar off the live stream — sixty bars before it could be read,
    which on a 1 MIN chart is an hour. Switching to that tab did not help: the
    browser already had the candles and asked for nothing, so the panel started
    the hour over from two candles with a fully drawn chart on screen beside
    it.
    """

    START = 1_786_662_000

    def _source(self):
        from poa.feed.source import FeedChartSource

        source = FeedChartSource(port=59999)
        # The user's three tabs. Several charts named at once is the page
        # describing its workspace, not the chart in front, so it claims none.
        source._handle(
            "saveCharts",
            ["saveCharts", {"settings": [
                {"symbol": "EURCHF_otc", "chartPeriod": 60},
                {"symbol": "EURUSD_otc"},
                {"symbol": "EURAUD_otc"},
            ]}],
        )
        # EUR/CHF is the one in front, and it is trading — so nothing weaker
        # than the page naming its own chart may take the panel off it.
        source._handle(
            "changeSymbol", ["changeSymbol", {"asset": "EURCHF_otc", "period": 60}]
        )
        source._handle("updateStream", [["EURCHF_otc", self.START, 0.9353]])
        return source

    def _history(self, asset, count=90, period=60, price=1.6):
        """Closed bars running up to START, which is where the stream begins.

        Genuinely in the past, because the builder leaves the bar that is still
        forming to the tick stream and drops history for it — so backfill that
        overlapped the live bar would be discarded, correctly, and prove
        nothing.
        """
        rows = [
            {"time": self.START - (count - i) * period, "open": price,
             "high": price, "low": price, "close": price, "volume": 1}
            for i in range(count)
        ]
        return ["loadHistoryPeriodFast", {"asset": asset, "period": period,
                                          "data": rows}]

    def test_history_for_a_tab_behind_is_kept_not_dropped(self):
        source = self._source()
        assert source._asset == "EURCHF_otc"

        source._handle("loadHistoryPeriodFast", self._history("EURAUD_otc"))

        builder = source._charts.get(("EURAUD_otc", 60))
        assert builder is not None, "the tab's history went in the bin"
        assert len(builder.settled) >= 60, (
            f"only {len(builder.settled)} candles kept; 60 are needed to read a chart"
        )

    def test_the_watchlist_offers_it_straight_away(self):
        source = self._source()
        source._handle("loadHistoryPeriodFast", self._history("EURAUD_otc"))

        watched = {name: series for name, _p, series in source.watched()}
        assert "EUR/AUD OTC" in watched
        assert len(watched["EUR/AUD OTC"]) >= 60

    def test_switching_to_that_tab_arrives_with_its_history(self):
        """The move that produced two candles and a full chart on screen."""
        source = self._source()
        source._handle("loadHistoryPeriodFast", self._history("EURAUD_otc"))
        source._handle(
            "saveCharts",
            ["saveCharts", {"settings": [{"symbol": "EURAUD_otc", "chartPeriod": 60}]}],
        )

        assert source._asset == "EURAUD_otc"
        assert len(source._builder.settled) >= 60

    def test_the_open_chart_still_takes_its_own_history(self):
        source = self._source()
        source._handle("loadHistoryPeriodFast", self._history("EURCHF_otc"))

        assert len(source._builder.settled) >= 60
        assert source._history_seen is True

    def test_an_instrument_outside_the_tabs_is_still_refused(self):
        """The socket ticks far more pairs than anyone trades. Keeping history
        for all of them would fill the slots with instruments nobody asked
        for."""
        source = self._source()
        source._handle("loadHistoryPeriodFast", self._history("BTCUSD_otc"))

        assert ("BTCUSD_otc", 60) not in source._charts

    def test_history_does_not_displace_the_open_chart(self):
        source = self._source()
        source._handle("loadHistoryPeriodFast", self._history("EURAUD_otc"))

        assert source._asset == "EURCHF_otc", "a tab behind must not steal the panel"

    def test_ticks_already_gathered_are_not_lost_to_the_backfill(self):
        from poa.feed.ticks import Tick

        source = self._source()
        for step in range(0, 120, 10):
            source._handle(
                "updateStream", [["EURAUD_otc", self.START + 5_000 + step, 1.63]]
            )
        before = len(source._charts[("EURAUD_otc", 60)].settled)
        source._handle("loadHistoryPeriodFast", self._history("EURAUD_otc"))
        after = len(source._charts[("EURAUD_otc", 60)].settled)

        assert after > before
