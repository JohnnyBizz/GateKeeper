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

        names = [asset for asset, _tf, _series in source.watched()]
        assert names == ["EUR/USD OTC", "GBP/USD OTC"]

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
        source = self._source()
        source.focus("GBP/USD OTC")
        source._charts.clear()
        assert source.capture().asset == "EUR/USD OTC"
        assert source._focus is None
