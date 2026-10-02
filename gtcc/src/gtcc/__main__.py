"""Command line entry points.

    python -m gtcc serve          start the API and dashboard
    python -m gtcc init-db        create tables (development; use Alembic otherwise)
    python -m gtcc create-user    create the owner account, prompting for a password
    python -m gtcc check          report configuration and adapter health, then exit
    python -m gtcc risk           state the risk limits in money, with a worked example
    python -m gtcc scan           analyse symbols and report what could not be read
    python -m gtcc record         save a venue's candles for replay
    python -m gtcc backtest       replay a recording through a strategy
    python -m gtcc oanda-check    verify the OANDA connection and response shapes
"""

from __future__ import annotations

import argparse
import getpass
import sys

from gtcc.bootstrap import build_runtime, init_database
from gtcc.config import describe_url, get_settings
from gtcc.logging_setup import configure_logging
from gtcc.scanner import SortKey


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gtcc", description="Grok Trading Command Center")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("init-db", help="create tables directly (development only)")
    sub.add_parser("check", help="report configuration and adapter health")
    risk = sub.add_parser(
        "risk", help="show what the configured risk limits mean in money"
    )
    risk.add_argument(
        "--equity", default="100000", help="account size to illustrate (default 100000)"
    )
    risk.add_argument("--currency", default="USD")
    risk.add_argument(
        "--example", action="store_true",
        help="read config/risk.example.yaml instead, to see it before adopting it",
    )
    scan = sub.add_parser(
        "scan", help="analyse a list of symbols and report what was found"
    )
    scan.add_argument("symbols", nargs="+", help="symbols as the venue names them")
    scan.add_argument("--timeframe", default="15m")
    scan.add_argument("--min-history", type=int, default=60)
    scan.add_argument(
        "--max-requests", type=int, default=None,
        help="ceiling on venue calls; symbols beyond it are reported unread",
    )
    scan.add_argument(
        "--sort", default="SIGNAL", choices=[key.value for key in SortKey],
    )
    rec = sub.add_parser(
        "record", help="save a venue's candles as a replayable recording"
    )
    rec.add_argument("symbols", nargs="+")
    rec.add_argument("--timeframe", default="15m")
    rec.add_argument("--limit", type=int, default=5000)
    rec.add_argument("--directory", default=None)
    rec.add_argument(
        "--overwrite", action="store_true",
        help="take the venue's version of bars that differ from the recording",
    )

    back = sub.add_parser(
        "backtest", help="replay a recording through a strategy and the risk engine"
    )
    back.add_argument("symbol")
    back.add_argument("--timeframe", default="15m")
    back.add_argument("--directory", default=None)
    back.add_argument("--strategy", default="trend_continuation")
    back.add_argument("--equity", default="100000")
    back.add_argument("--warmup", type=int, default=120)
    back.add_argument("--spread-bps", default="1.0")
    back.add_argument("--slippage-bps", default="1.0")
    back.add_argument("--commission-bps", default="1.0")
    back.add_argument(
        "--segment", default="in_sample",
        choices=["in_sample", "validation", "out_of_sample", "all"],
        help="which chronological segment to run; defaults to in-sample",
    )
    back.add_argument(
        "--stress", action="store_true",
        help="also run the cost sweep and the other robustness checks",
    )
    oanda = sub.add_parser(
        "oanda-check",
        help="verify the OANDA connection and response shapes (read-only)",
    )
    oanda.add_argument(
        "--symbol", default="EUR_USD", help="instrument to probe (default EUR_USD)"
    )

    create = sub.add_parser("create-user", help="create an account")
    create.add_argument("email")
    create.add_argument("--role", default="owner")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if args.command == "init-db":
        init_database(settings)
        print(f"tables created in {describe_url(settings.database_dsn)}")
        return 0

    if args.command == "create-user":
        from gtcc.api.security import hash_password
        from gtcc.storage import db
        from gtcc.storage.models import User

        db.configure(settings.database_dsn)
        password = getpass.getpass("password (minimum 12 characters): ")
        if password != getpass.getpass("repeat: "):
            print("passwords do not match", file=sys.stderr)
            return 1
        with db.session_scope() as session:
            session.add(
                User(
                    email=args.email.strip().lower(),
                    password_hash=hash_password(password),
                    role=args.role,
                )
            )
        print(f"created {args.email} with role {args.role}")
        return 0

    if args.command == "risk":
        from pathlib import Path as _Path

        from gtcc.domain.money import D as _D
        from gtcc.risk.explain import explain, worked_example
        from gtcc.risk.limits import RiskConfigError, load_limits

        path = (
            _Path("config/risk.example.yaml") if args.example else settings.risk_config_path
        )
        try:
            limits = load_limits(path, allow_example=args.example)
        except RiskConfigError as exc:
            print(exc)
            return 2

        print(f"Risk limits from {path}\n")
        for line in explain(limits, equity=_D(args.equity), currency=args.currency):
            print(line)
        print()
        for line in worked_example(limits, equity=_D(args.equity), currency=args.currency):
            print(line)
        return 0

    if args.command == "scan":
        from gtcc.domain.enums import Timeframe
        from gtcc.scanner import ScanSettings
        from gtcc.scanner.report import render

        runtime = build_runtime(settings)
        scanner = runtime.scanner(
            ScanSettings(
                timeframe=Timeframe(args.timeframe),
                min_history=args.min_history,
                max_requests=args.max_requests,
            )
        )
        result = scanner.scan(args.symbols, now=runtime.clock())
        for line in render(result, sort_by=SortKey(args.sort)):
            print(line)
        return 0

    if args.command == "record":
        from pathlib import Path as _P

        from gtcc.data.recorder import record as _record
        from gtcc.domain.enums import Timeframe

        runtime = build_runtime(settings)
        timeframe = Timeframe(args.timeframe)
        failures = 0
        for symbol in args.symbols:
            try:
                report = _record(
                    runtime.data,
                    _P(args.directory or settings.recordings_path),
                    symbol, timeframe,
                    limit=args.limit, overwrite=args.overwrite,
                )
            except Exception as exc:  # noqa: BLE001 - reported, not hidden
                failures += 1
                print(f"{symbol}: FAILED — {exc}")
                continue
            for line in report.describe():
                print(line)
        if failures:
            print(f"\n{failures} symbol(s) failed. Nothing was invented for them.")
            return 1
        return 0

    if args.command == "backtest":
        from pathlib import Path as _P

        from gtcc.adapters.replay import ReplayAdapter
        from gtcc.backtest import (
            BacktestSettings,
            Backtester,
            BarCosts,
            assess,
            rising_costs,
            split,
        )
        from gtcc.backtest.report import render
        from gtcc.data.recorder import read_sidecar, spec_from_dict
        from gtcc.domain.enums import Timeframe
        from gtcc.domain.money import D as _D
        from gtcc.risk.limits import RiskConfigError, load_limits

        timeframe = Timeframe(args.timeframe)
        directory = _P(args.directory or settings.recordings_path)
        adapter = ReplayAdapter(directory=directory)
        try:
            bars = adapter.get_bars(args.symbol, timeframe, limit=0)
        except Exception as exc:  # noqa: BLE001
            print(f"no recording to replay: {exc}")
            print(
                f"Record one first:  python -m gtcc record {args.symbol} "
                f"--timeframe {args.timeframe}"
            )
            return 2
        if not bars:
            print(f"the recording for {args.symbol} {timeframe} is empty")
            return 2

        try:
            limits = load_limits(settings.risk_config_path)
        except RiskConfigError as exc:
            print(exc)
            return 2

        runtime = build_runtime(settings)
        try:
            strategy = runtime.strategies.get(args.strategy)
        except KeyError as exc:
            print(exc)
            return 2

        # Prefer the specification recorded WITH the data: it is what was
        # true when the bars were taken, and it means a backtest needs no
        # live connection. Falling back to the live venue is second best.
        instrument = None
        try:
            sidecar = read_sidecar(directory, args.symbol, timeframe)
            if "instrument" in sidecar:
                instrument = spec_from_dict(sidecar["instrument"])
                print(
                    f"using the contract specification recorded with the data "
                    f"({sidecar.get('venue', 'unknown venue')}, "
                    f"{sidecar.get('recorded_at', 'unknown date')})."
                )
        except (FileNotFoundError, ValueError) as exc:
            print(f"no recorded specification: {exc}")

        if instrument is None:
            try:
                instrument = runtime.data.get_instrument(args.symbol)
                print("using the venue's CURRENT contract specification.")
            except Exception as exc:  # noqa: BLE001
                print(f"no contract specification for {args.symbol}: {exc}")
                print(
                    "A backtest cannot size a position without a real tick size "
                    "and lot step, and guessing them would mis-size every trade. "
                    f"Re-record it:  python -m gtcc record {args.symbol} "
                    f"--timeframe {args.timeframe}"
                )
                return 2

        costs = BarCosts(
            spread_bps=_D(args.spread_bps),
            slippage_bps=_D(args.slippage_bps),
            commission_bps=_D(args.commission_bps),
        )
        if args.segment == "all":
            series = list(bars)
            print(
                "Running the WHOLE series. Nothing is held out, so these numbers "
                "cannot tell you whether the strategy generalises.\n"
            )
        else:
            parts = split(bars)
            series = list(getattr(parts, args.segment))
            print(f"{parts.describe()}; running {args.segment}.\n")

        backtester = Backtester(limits=limits)
        bt_settings = BacktestSettings(
            symbol=args.symbol, timeframe=timeframe, warmup_bars=args.warmup,
            starting_equity=_D(args.equity), costs=costs,
        )
        result = backtester.run(strategy, series, instrument, bt_settings)

        robustness = None
        if args.stress:
            robustness = assess(
                result,
                cost_runner=lambda c: backtester.run(
                    strategy, series, instrument,
                    BacktestSettings(
                        symbol=args.symbol, timeframe=timeframe,
                        warmup_bars=args.warmup, starting_equity=_D(args.equity),
                        costs=c,
                    ),
                ),
                cost_levels=rising_costs(costs),
            )
        for line in render(result, robustness):
            print(line)
        return 0

    if args.command == "oanda-check":
        from gtcc.adapters import oanda_check

        return oanda_check.run(settings)

    if args.command == "check":
        runtime = build_runtime(settings)
        broker = runtime.broker.health()
        data = runtime.data.health()
        execution = runtime.ensure_execution()
        print(f"mode                 {execution.mode}")
        print(f"live permitted here  {settings.allow_live_trading}  (deployment permission)")
        print(f"live ARMED           {execution.live_armed}  (runtime; always false at startup)")
        print(f"breaker              {'TRIPPED' if execution.tripped else 'clear'}")
        print(f"automatic execution  {settings.automatic_execution}")
        print(f"database             {describe_url(settings.database_dsn)['scheme']}")
        print(f"grok model           {settings.grok_model or '(not configured)'}")
        print(f"risk limits          {settings.risk_config_path}"
              f"{' [EXAMPLE]' if runtime.limits.is_example else ''}")
        print(f"broker  {runtime.broker.name:10} {'ok' if broker.healthy else 'DOWN'}  {broker.detail}")
        print(f"data    {runtime.data.name:10} {'ok' if data.healthy else 'DOWN'}  {data.detail}")
        return 0 if broker.healthy and data.healthy else 1

    if args.command == "serve":
        import uvicorn

        from gtcc.api.app import create_app
        from gtcc.storage import db

        db.configure(settings.database_dsn)
        app = create_app(settings=settings, runtime=build_runtime(settings))
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
