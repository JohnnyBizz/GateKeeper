"""Verify the OANDA adapter against the real API.

This exists because of an honest limitation. The adapter was written
without access to OANDA's documentation, so the response field names it
expects are an assumption. This command turns that assumption into a
few seconds of checking that the account owner performs themselves.

It calls each endpoint the adapter uses, reports which expected fields
were present and which were not, and prints a sample of the values so
the units can be eyeballed. It is read-only: it places no order,
cancels nothing and modifies nothing.

The token is never printed, never written to a file, and never included
in any output. Only whether one is set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from gtcc.adapters.errors import AdapterError
from gtcc.adapters.oanda import GRANULARITY, OandaClient, OandaDataAdapter, OandaBroker
from gtcc.domain.enums import Timeframe

#: What the adapter reads, per endpoint. Keep in step with oanda.py.
EXPECTED: dict[str, tuple[str, ...]] = {
    "accounts/{id}/summary": (
        "account.id", "account.currency", "account.NAV", "account.balance",
        "account.marginUsed", "account.marginAvailable",
    ),
    "accounts/{id}/instruments": (
        "instruments[].name", "instruments[].type", "instruments[].pipLocation",
        "instruments[].displayPrecision", "instruments[].tradeUnitsPrecision",
        "instruments[].minimumTradeSize", "instruments[].marginRate",
    ),
    "accounts/{id}/pricing": (
        "prices[].time", "prices[].tradeable", "prices[].bids[].price",
        "prices[].bids[].liquidity", "prices[].asks[].price",
    ),
    "instruments/{symbol}/candles": (
        "candles[].time", "candles[].complete", "candles[].volume",
        "candles[].mid.o", "candles[].mid.h", "candles[].mid.l", "candles[].mid.c",
    ),
    "accounts/{id}/openPositions": (
        "positions[].instrument", "positions[].long.units",
        "positions[].long.averagePrice", "positions[].long.pl",
        "positions[].long.financing",
    ),
}


@dataclass
class EndpointResult:
    endpoint: str
    reached: bool
    error: str = ""
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    sample: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.reached and not self.missing


def _probe(payload: Any, path: str) -> tuple[bool, Any]:
    """Walk a path like ``prices[].bids[].price`` through a response."""
    current: Any = payload
    for part in path.split("."):
        if part.endswith("[]"):
            key = part[:-2]
            if key:
                if not isinstance(current, Mapping) or key not in current:
                    return False, None
                current = current[key]
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes)):
                return False, None
            if not current:
                # Present but empty. Not a missing field: an account with
                # no open positions is the normal case.
                return True, "<empty list>"
            current = current[0]
        else:
            if not isinstance(current, Mapping) or part not in current:
                return False, None
            current = current[part]
    return True, current


def check(client: OandaClient, *, symbol: str = "EUR_USD") -> list[EndpointResult]:
    """Probe every endpoint the adapter uses. Read-only throughout."""
    account = client.account_id
    calls = [
        ("accounts/{id}/summary", "GET", f"/v3/accounts/{account}/summary", None),
        ("accounts/{id}/instruments", "GET", f"/v3/accounts/{account}/instruments", None),
        (
            "accounts/{id}/pricing", "GET", f"/v3/accounts/{account}/pricing",
            {"instruments": symbol},
        ),
        (
            "instruments/{symbol}/candles", "GET", f"/v3/instruments/{symbol}/candles",
            {"granularity": "M5", "price": "M", "count": 3},
        ),
        ("accounts/{id}/openPositions", "GET", f"/v3/accounts/{account}/openPositions", None),
    ]

    results: list[EndpointResult] = []
    for label, method, path, params in calls:
        try:
            payload = client.request(method, path, params=params)
        except AdapterError as exc:
            results.append(EndpointResult(endpoint=label, reached=False, error=str(exc)))
            continue

        result = EndpointResult(endpoint=label, reached=True)
        for expected in EXPECTED[label]:
            found, value = _probe(payload, expected)
            if found:
                result.present.append(expected)
                result.sample[expected] = value
            else:
                result.missing.append(expected)
        results.append(result)
    return results


def report(results: Sequence[EndpointResult], *, environment: str, account_id: str) -> int:
    """Print the findings. Returns a process exit code."""
    print(f"OANDA {environment} · account {account_id}")
    print("read-only check: no order is placed, nothing is modified\n")

    broken = 0
    for result in results:
        if not result.reached:
            print(f"  UNREACHABLE  {result.endpoint}")
            print(f"               {result.error}")
            broken += 1
            continue

        marker = "ok" if result.ok else "FIELDS MISSING"
        print(f"  {marker:14} {result.endpoint}")
        for expected in result.present:
            value = result.sample[expected]
            rendered = str(value)
            if len(rendered) > 42:
                rendered = rendered[:39] + "..."
            print(f"                 {expected:42} = {rendered}")
        for expected in result.missing:
            print(f"                 {expected:42} MISSING")
        if result.missing:
            broken += 1
        print()

    if broken:
        print(
            f"{broken} endpoint(s) did not match what the adapter expects.\n"
            "The adapter's field names came from secondary sources rather than\n"
            "OANDA's own documentation, so a mismatch here is expected to be a\n"
            "naming difference rather than a problem with your account. Send the\n"
            "output above and the mapping can be corrected; it is a small change\n"
            "in gtcc/adapters/oanda.py and tests/test_oanda.py."
        )
        return 1

    print(
        "Every field the adapter reads was present. The response shapes it was\n"
        "written against are correct, and the instrument specifications it will\n"
        "size positions from are coming from your account rather than from any\n"
        "value written into this codebase."
    )
    return 0


def run(settings) -> int:
    """Entry point for ``python -m gtcc oanda-check``."""
    if not settings.oanda_configured:
        print(
            "OANDA is not configured.\n\n"
            "Set these in the server environment or in .env, never in a chat\n"
            "message or a commit:\n\n"
            "  GTCC_OANDA_TOKEN=<your v20 personal access token>\n"
            "  GTCC_OANDA_ACCOUNT_ID=<e.g. 101-004-1234567-001>\n"
            "  GTCC_OANDA_ENVIRONMENT=practice\n\n"
            "Generate the token from your OANDA account's own API access page.\n"
            "It is not your username and password."
        )
        return 2

    client = OandaClient(
        token=settings.oanda_token.get_secret_value(),
        account_id=settings.oanda_account_id,
        environment=settings.oanda_environment,
        timeout_seconds=settings.oanda_timeout_seconds,
    )
    client.limiter.per_second = settings.oanda_requests_per_second
    try:
        results = check(client)
        code = report(
            results,
            environment=settings.oanda_environment,
            account_id=settings.oanda_account_id,
        )
        if code == 0:
            _summarise_instruments(client, settings)
        return code
    finally:
        client.close()


def _summarise_instruments(client: OandaClient, settings) -> None:
    """Show what the account may actually trade, and how it is specified."""
    data = OandaDataAdapter(client=client)
    try:
        specs = data.refresh_instruments()
    except AdapterError as exc:
        print(f"\ncould not read the instrument list: {exc}")
        return

    print(f"\n{len(specs)} tradeable instruments. A sample, as the platform sees them:\n")
    print(f"  {'symbol':12} {'tick':>10} {'pip':>8} {'lot step':>9} {'min':>7} {'max lev':>8}")
    for symbol in sorted(specs)[:8]:
        spec = specs[symbol]
        print(
            f"  {symbol:12} {spec.tick_size:>10} {str(spec.pip_size or '-'):>8} "
            f"{spec.lot_step:>9} {spec.min_qty:>7} {spec.max_leverage:>7.1f}x"
        )

    supported = sorted(str(t) for t in GRANULARITY)
    print(f"\n  timeframes available: {', '.join(supported)}")
    broker = OandaBroker(client=client, data=data)
    print(f"  trading mode for this account: {broker.mode}")
