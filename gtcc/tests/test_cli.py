"""The commands themselves, invoked the way a person invokes them.

Worth having because the suite missed a real bug here: a local
`from gtcc.bootstrap import build_runtime` inside one branch of `main()`
made the name a local for the whole function, so every OTHER branch raised
UnboundLocalError. Nothing in 657 unit tests touched it, because nothing
called `main()`. Only running the real command found it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gtcc.__main__ import main
from gtcc.config import Settings, set_settings
from gtcc.data.recorder import write_recording
from gtcc.domain.enums import AssetClass, Market, Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar
from gtcc.domain.money import D
from gtcc.storage import db

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    """Settings installed as the process-wide ones, as `main` reads them."""
    import secrets

    from tests.conftest import write_risk_config

    set_settings(None)
    created = Settings(
        secret_key=secrets.token_urlsafe(48),
        database_url=f"sqlite:///{tmp_path / 'cli.db'}",
        environment="development",
        log_level="CRITICAL",
        log_format="text",
        risk_config_path=write_risk_config(tmp_path),
    )
    set_settings(created)
    yield created
    set_settings(None)
    # Leave no configured engine behind: the next test gets its own path.
    db._engine = None


@pytest.fixture
def recording(tmp_path):
    """A recording on disk, with its contract specification."""
    spec = InstrumentSpec(
        symbol="TESTPAIR", market=Market.STOCKS, asset_class=AssetClass.EQUITY,
        quote_currency="USD", tick_size=D("0.01"), lot_step=D("1"), min_qty=D("1"),
    )
    bars = [
        Bar(
            symbol="TESTPAIR", timeframe=Timeframe.M15,
            timestamp=START + timedelta(minutes=15 * index),
            open=D(f"{100 + index * 0.2:.2f}"),
            high=D(f"{100 + index * 0.2 + 0.4:.2f}"),
            low=D(f"{100 + index * 0.2 - 0.4:.2f}"),
            close=D(f"{100 + index * 0.2:.2f}"),
            volume=D("5000"), closed=True,
        )
        for index in range(400)
    ]
    directory = tmp_path / "recordings"
    write_recording(
        directory, "TESTPAIR", Timeframe.M15, bars, venue="test", spec=spec
    )
    return directory


class TestEveryCommandIsReachable:
    """The regression that motivated this file."""

    def test_risk_runs(self, cli_settings, capsys):
        assert main(["risk", "--equity", "50000"]) == 0
        assert "On an account of USD 50,000.00" in capsys.readouterr().out

    def test_scan_runs(self, cli_settings, capsys):
        assert main(["scan", "NOSUCHTHING"]) == 0
        assert "NOT ANALYSED" in capsys.readouterr().out

    def test_backtest_runs(self, cli_settings, recording, capsys):
        code = main(
            [
                "backtest", "TESTPAIR",
                "--directory", str(recording),
                "--strategy", "trend_continuation",
                "--warmup", "60",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "in-sample" in out
        assert "recorded with the data" in out

    def test_check_runs(self, cli_settings, capsys):
        assert main(["check"]) == 0

    def test_oanda_check_without_a_token_says_so(self, cli_settings, capsys):
        code = main(["oanda-check"])
        out = capsys.readouterr().out

        assert "not configured" in out.lower() or code != 0


class TestTheBacktestCommandRefusesRatherThanGuessing:
    def test_a_missing_recording_explains_how_to_make_one(
        self, cli_settings, tmp_path, capsys
    ):
        code = main(["backtest", "NOTHING", "--directory", str(tmp_path / "none")])

        assert code == 2
        out = capsys.readouterr().out
        assert "no recording to replay" in out
        assert "python -m gtcc record NOTHING" in out

    def test_an_unknown_strategy_lists_what_is_registered(
        self, cli_settings, recording, capsys
    ):
        code = main(
            [
                "backtest", "TESTPAIR", "--directory", str(recording),
                "--strategy", "no_such_strategy",
            ]
        )

        assert code == 2
        assert "trend_continuation" in capsys.readouterr().out

    def test_a_recording_with_no_spec_refuses_to_guess_one(
        self, cli_settings, tmp_path, capsys
    ):
        """A guessed tick size would mis-size every position in the run."""
        bars = [
            Bar(
                symbol="NOSPEC", timeframe=Timeframe.M15,
                timestamp=START + timedelta(minutes=15 * index),
                open=D("100"), high=D("101"), low=D("99"), close=D("100"),
                volume=D("1000"), closed=True,
            )
            for index in range(200)
        ]
        directory = tmp_path / "nospec"
        write_recording(directory, "NOSPEC", Timeframe.M15, bars)

        code = main(["backtest", "NOSPEC", "--directory", str(directory)])

        assert code == 2
        out = capsys.readouterr().out
        assert "no contract specification" in out
        assert "mis-size every trade" in out

    def test_running_the_whole_series_warns_that_nothing_is_held_out(
        self, cli_settings, recording, capsys
    ):
        code = main(
            [
                "backtest", "TESTPAIR", "--directory", str(recording),
                "--segment", "all", "--warmup", "60",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "Nothing is held out" in out
        assert "cannot tell you whether the strategy generalises" in out

    def test_without_stress_the_report_says_nothing_attacked_it(
        self, cli_settings, recording, capsys
    ):
        main(["backtest", "TESTPAIR", "--directory", str(recording), "--warmup", "60"])

        out = capsys.readouterr().out
        assert "No robustness check was run" in out
        assert "most favourable reading available" in out


class TestTheRecordCommand:
    def test_recording_without_a_venue_reports_the_failure(
        self, cli_settings, tmp_path, capsys
    ):
        """No token configured means the replay adapter, which has no symbol."""
        code = main(
            [
                "record", "EUR_USD",
                "--directory", str(tmp_path / "out"),
            ]
        )

        out = capsys.readouterr().out
        assert code == 1
        assert "FAILED" in out
        assert "Nothing was invented" in out
