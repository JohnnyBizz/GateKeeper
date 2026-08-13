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

    def test_non_page_targets_are_ignored(self):
        assert Target.from_json({"type": "service_worker", "webSocketDebuggerUrl": "ws://x"}) is None
        assert Target.from_json({"type": "page", "id": "1"}) is None
