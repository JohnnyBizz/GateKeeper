"""The Backtest Lab page.

A page that lists recordings and replays one. The properties worth pinning
are the refusals: no recordings is said plainly rather than shown as an
empty table, a recording with no contract specification is listed as
unusable rather than hidden, and a result rendered without the robustness
checks says that nothing attacked it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gtcc.data.recorder import write_recording
from gtcc.domain.enums import AssetClass, Market, Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar
from gtcc.domain.money import D

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _spec(symbol: str = "LABTEST") -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, market=Market.STOCKS, asset_class=AssetClass.EQUITY,
        quote_currency="USD", tick_size=D("0.01"), lot_step=D("1"), min_qty=D("1"),
    )


def _bars(symbol: str = "LABTEST", count: int = 400) -> list[Bar]:
    return [
        Bar(
            symbol=symbol, timeframe=Timeframe.M15,
            timestamp=START + timedelta(minutes=15 * index),
            open=D(f"{100 + index * 0.2:.2f}"),
            high=D(f"{100 + index * 0.2 + 0.4:.2f}"),
            low=D(f"{100 + index * 0.2 - 0.4:.2f}"),
            close=D(f"{100 + index * 0.2:.2f}"),
            volume=D("5000"), closed=True,
        )
        for index in range(count)
    ]


@pytest.fixture
def lab_settings(settings, tmp_path, monkeypatch):
    """Point the deployment's recordings path at a directory this test owns."""
    from dataclasses import replace as _replace

    from gtcc.config import set_settings

    directory = tmp_path / "recordings"
    directory.mkdir()
    updated = settings.model_copy(update={"recordings_path": directory})
    set_settings(updated)
    yield updated, directory
    set_settings(settings)


@pytest.fixture
def lab_api(lab_settings, runtime):
    """An app whose settings carry the test's recordings path.

    The shared `runtime` fixture has an empty strategy registry — only
    `build_runtime` registers the shipped strategy — so this registers one
    explicitly rather than relying on bootstrap wiring the page does not use.
    """
    from gtcc.strategies.trend_continuation import TrendContinuation
    from tests.conftest import _build_app, _sign_in
    from tests.support import OWNER_EMAIL, OWNER_PASSWORD

    runtime.strategies.register(TrendContinuation())
    updated, _ = lab_settings
    app = _build_app(updated, runtime)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    _sign_in(client, OWNER_EMAIL, OWNER_PASSWORD)
    return client


class TestTheEmptyState:
    def test_no_recordings_says_how_to_make_one(self, lab_api):
        text = lab_api.get("/backtest").text

        assert "No recordings in" in text
        assert "python -m gtcc record" in text
        assert "will not invent a series" in text

    def test_there_is_no_run_form_with_nothing_to_run(self, lab_api):
        text = lab_api.get("/backtest").text

        assert "Attack the result" not in text


class TestListingRecordings:
    def test_a_recording_is_listed_with_its_provenance(self, lab_api, lab_settings):
        _, directory = lab_settings
        write_recording(
            directory, "LABTEST", Timeframe.M15, _bars(),
            venue="test-venue", spec=_spec(),
        )

        text = lab_api.get("/backtest").text

        assert "LABTEST" in text
        assert "test-venue" in text
        assert ">400<" in text or "400" in text

    def test_a_recording_without_a_spec_is_listed_as_unusable(
        self, lab_api, lab_settings
    ):
        """Hidden would be worse: the owner put the file there."""
        _, directory = lab_settings
        write_recording(directory, "NOSPEC", Timeframe.M15, _bars("NOSPEC"))

        text = lab_api.get("/backtest").text

        assert "NOSPEC" in text
        assert "no contract spec" in text
        assert "mis-size every position" in text


class TestRunningOne:
    def test_a_run_renders_the_report(self, lab_api, lab_settings):
        _, directory = lab_settings
        write_recording(
            directory, "LABTEST", Timeframe.M15, _bars(), venue="v", spec=_spec()
        )

        text = lab_api.get(
            "/backtest?recording=LABTEST|15m&strategy=trend_continuation"
            "&segment=in_sample&warmup=60"
        ).text

        assert "in-sample" in text or "Result" in text
        assert "assumed costs" in text

    def test_a_run_without_stress_says_nothing_attacked_it(
        self, lab_api, lab_settings
    ):
        _, directory = lab_settings
        write_recording(
            directory, "LABTEST", Timeframe.M15, _bars(), venue="v", spec=_spec()
        )

        text = lab_api.get(
            "/backtest?recording=LABTEST|15m&strategy=trend_continuation&warmup=60"
        ).text

        assert "nothing attacked it" in text
        assert "most favourable reading available" in text

    def test_an_unknown_timeframe_is_an_error_not_a_blank_page(
        self, lab_api, lab_settings
    ):
        _, directory = lab_settings
        write_recording(
            directory, "LABTEST", Timeframe.M15, _bars(), venue="v", spec=_spec()
        )

        text = lab_api.get("/backtest?recording=LABTEST|99y").text

        assert "Could not run it" in text
        assert "not a timeframe" in text

    def test_a_missing_recording_is_an_error_not_a_blank_page(
        self, lab_api, lab_settings
    ):
        _, directory = lab_settings
        write_recording(
            directory, "LABTEST", Timeframe.M15, _bars(), venue="v", spec=_spec()
        )

        text = lab_api.get("/backtest?recording=GHOST|15m").text

        assert "Could not run it" in text

    def test_the_page_needs_a_session(self, lab_settings, runtime):
        from fastapi.testclient import TestClient

        from tests.conftest import _build_app

        updated, _ = lab_settings
        client = TestClient(_build_app(updated, runtime), follow_redirects=False)

        assert client.get("/backtest").status_code in (302, 303, 307, 401, 403)
