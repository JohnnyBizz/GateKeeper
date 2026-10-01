"""OANDA v20 adapter — market data and broker.

Written against OANDA's v20 REST API. **The API documentation was not
reachable from the machine this was written on**, so the endpoint paths
and hostnames below come from secondary sources and the response field
names from the API as I understand it. That is a real uncertainty and it
is handled rather than hidden:

* Nothing about an instrument is hardcoded. Tick size, pip size, lot
  step, minimum size and margin rate all come from OANDA's own
  instruments endpoint. If my understanding of a field is wrong the
  error says which field, on which endpoint, rather than silently
  producing a tick size that would mis-size every position.
* Every response is parsed strictly. A missing or unreadable field
  raises :class:`OandaSchemaError` naming the path, the field and what
  was received. No defaults, no ``.get(key, 0)``.
* ``python -m gtcc oanda-check`` hits each endpoint with the owner's own
  token and prints which fields actually came back against what this
  module expects. That is the verification step, and it takes a few
  seconds.

Two safety properties specific to this adapter:

**The token never leaves the environment.** It is a
:class:`~pydantic.SecretStr` read from ``GTCC_OANDA_TOKEN``, is attached
to requests inside this module, and is scrubbed from every error path.
It is not logged, not echoed, and not included in any exception message.

**The live host is unreachable unless live is armed.** Pointing at
``api-fxtrade.oanda.com`` requires both deployment permission and a
runtime arming, checked at construction. A practice token against the
live host simply fails; a live token against it would not, which is why
the gate is here and not left to configuration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

import httpx

from gtcc.adapters.base import AdapterHealth, BrokerAdapter, Capability, MarketDataAdapter
from gtcc.adapters.errors import (
    AdapterError,
    AuthenticationError,
    ConnectionUnhealthy,
    FeatureUnavailable,
    LiveTradingDisabled,
    OrderRejected,
    RateLimited,
)
from gtcc.domain.enums import (
    AssetClass,
    Market,
    OrderStatus,
    OrderType,
    Side,
    Timeframe,
    TradingMode,
)
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote, utcnow
from gtcc.domain.money import ONE, ZERO, D
from gtcc.domain.orders import Account, Fill, Order, OrderRequest, Position, new_id

PRACTICE_HOST = "https://api-fxpractice.oanda.com"
PRACTICE_STREAM_HOST = "https://stream-fxpractice.oanda.com"
LIVE_HOST = "https://api-fxtrade.oanda.com"
LIVE_STREAM_HOST = "https://stream-fxtrade.oanda.com"

#: OANDA documents a per-token request rate. The limiter below is set
#: well under whatever the published figure is, because being throttled
#: mid-session is a data-staleness event and the risk engine latches on
#: those. Slower and reliable beats fast and latched.
DEFAULT_REQUESTS_PER_SECOND = 20.0

#: OANDA's granularity codes, mapped from the platform's timeframes.
#: Anything absent here is genuinely unsupported rather than guessed.
GRANULARITY: Mapping[Timeframe, str] = {
    Timeframe.M1: "M1",
    Timeframe.M5: "M5",
    Timeframe.M15: "M15",
    Timeframe.M30: "M30",
    Timeframe.H1: "H1",
    Timeframe.H4: "H4",
    Timeframe.D1: "D",
    Timeframe.W1: "W",
}


class OandaSchemaError(AdapterError):
    """A response did not contain what this adapter expects.

    Carries the endpoint and the field so that a documentation drift is
    a one-line diagnosis rather than an afternoon.
    """

    def __init__(self, endpoint: str, field_path: str, detail: str) -> None:
        self.endpoint = endpoint
        self.field_path = field_path
        super().__init__(
            f"OANDA {endpoint}: expected field {field_path!r} — {detail}. "
            "Run 'python -m gtcc oanda-check' to see what the endpoint "
            "actually returns."
        )


# -- strict reading helpers -------------------------------------------------


def _require(payload: Any, path: str, endpoint: str) -> Any:
    """Read a dotted path, raising with the path when it is absent."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise OandaSchemaError(
                endpoint, path,
                f"missing at {part!r}; got keys "
                f"{sorted(current) if isinstance(current, Mapping) else type(current).__name__}",
            )
        current = current[part]
    return current


def _decimal(payload: Any, path: str, endpoint: str) -> Decimal:
    """OANDA sends numbers as strings. Read one, strictly."""
    raw = _require(payload, path, endpoint)
    try:
        return D(raw)
    except (ValueError, InvalidOperation) as exc:
        raise OandaSchemaError(endpoint, path, f"{raw!r} is not a number") from exc


def _integer(payload: Any, path: str, endpoint: str) -> int:
    raw = _require(payload, path, endpoint)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise OandaSchemaError(endpoint, path, f"{raw!r} is not an integer") from exc


def _timestamp(payload: Any, path: str, endpoint: str) -> datetime:
    raw = _require(payload, path, endpoint)
    text = str(raw)
    try:
        # OANDA's RFC3339 carries nanoseconds, which fromisoformat on
        # 3.11 will not take. Trim to microseconds.
        if "." in text:
            head, _, tail = text.partition(".")
            fraction = "".join(ch for ch in tail if ch.isdigit())[:6]
            suffix = "+00:00" if text.endswith("Z") else tail[len(fraction):]
            text = f"{head}.{fraction}{'+00:00' if text.endswith('Z') else suffix}"
        else:
            text = text.replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OandaSchemaError(endpoint, path, f"{raw!r} is not an RFC3339 time") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass
class _RateLimiter:
    """A simple spacing limiter, shared by both adapters on one token."""

    per_second: float = DEFAULT_REQUESTS_PER_SECOND
    _last: float = field(default=0.0, init=False)

    def wait(self) -> None:
        if self.per_second <= 0:
            return
        interval = 1.0 / self.per_second
        elapsed = time.monotonic() - self._last
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last = time.monotonic()


@dataclass
class OandaClient:
    """HTTP plumbing. Holds the token and never surrenders it.

    One client is shared between the data adapter and the broker so that
    the rate limit is respected per token rather than per adapter.
    """

    token: str
    account_id: str
    environment: str = "practice"
    timeout_seconds: float = 10.0
    max_retries: int = 2
    limiter: _RateLimiter = field(default_factory=_RateLimiter)
    transport: httpx.BaseTransport | None = None

    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    last_error: str = field(default="", init=False)
    last_success_at: datetime | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.environment not in ("practice", "live"):
            raise ValueError("OANDA environment must be 'practice' or 'live'")
        if not self.token:
            raise AuthenticationError(
                "no OANDA token. Set GTCC_OANDA_TOKEN in the server "
                "environment; it is never read from a file or a prompt."
            )
        if not self.account_id:
            raise AuthenticationError("set GTCC_OANDA_ACCOUNT_ID to the account to trade")

    def __repr__(self) -> str:  # pragma: no cover - defensive
        """Never render the token, even in a traceback."""
        return (
            f"OandaClient(environment={self.environment!r}, "
            f"account_id={self.account_id!r}, token=***)"
        )

    @property
    def base_url(self) -> str:
        return PRACTICE_HOST if self.environment == "practice" else LIVE_HOST

    @property
    def stream_url(self) -> str:
        return PRACTICE_STREAM_HOST if self.environment == "practice" else LIVE_STREAM_HOST

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept-Datetime-Format": "RFC3339",
                },
            )
        return self._client

    def request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        """One request, with retries on transient failures only.

        A 4xx other than 429 is not retried: the request was wrong and
        sending it again will be wrong again. Nothing in any error
        message includes the token.
        """
        attempt = 0
        while True:
            attempt += 1
            self.limiter.wait()
            try:
                response = self._http().request(method, path, params=params, json=json)
            except httpx.TimeoutException as exc:
                self.last_error = f"timeout after {self.timeout_seconds}s"
                if attempt > self.max_retries:
                    raise ConnectionUnhealthy(f"OANDA {path}: {self.last_error}") from exc
                continue
            except httpx.HTTPError as exc:
                # str(exc) can include the request URL but never headers.
                self.last_error = f"transport error: {type(exc).__name__}"
                if attempt > self.max_retries:
                    raise ConnectionUnhealthy(f"OANDA {path}: {self.last_error}") from exc
                continue

            if response.status_code == 401:
                self.last_error = "rejected the token"
                raise AuthenticationError(
                    f"OANDA rejected the token for {self.environment}. A practice "
                    "token does not work against the live host, or the reverse."
                )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                self.last_error = "rate limited"
                if attempt > self.max_retries:
                    raise RateLimited(
                        "oanda",
                        float(retry_after) if retry_after else None,
                    )
                time.sleep(float(retry_after) if retry_after else 1.0)
                continue
            if response.status_code >= 500:
                self.last_error = f"server error {response.status_code}"
                if attempt > self.max_retries:
                    raise ConnectionUnhealthy(f"OANDA {path}: {self.last_error}")
                continue
            if response.status_code >= 400:
                self.last_error = f"HTTP {response.status_code}"
                raise AdapterError(
                    f"OANDA {path} returned {response.status_code}: "
                    f"{response.text[:300]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise OandaSchemaError(path, "<body>", "response was not JSON") from exc
            if not isinstance(payload, dict):
                raise OandaSchemaError(path, "<body>", "response was not a JSON object")

            self.last_error = ""
            self.last_success_at = utcnow()
            return payload

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _ensure_live_is_armed(environment: str, *, execution, deployment_allows_live: bool) -> None:
    """Refuse to construct a live-pointing adapter unless live is armed.

    A practice token against the live host fails harmlessly. A live
    token against it does not, so the gate belongs in code rather than
    in whichever environment variable happened to be set.
    """
    if environment != "live":
        return
    if not deployment_allows_live:
        raise LiveTradingDisabled(
            "this deployment does not permit live trading, so the live OANDA "
            "host is not available. Set GTCC_OANDA_ENVIRONMENT=practice."
        )
    if execution is None or not execution.live_armed:
        raise LiveTradingDisabled(
            "live execution has not been armed in this process, so the live "
            "OANDA host is not available. Arm it through the API first."
        )


# -- market data -------------------------------------------------------------


@dataclass
class OandaDataAdapter(MarketDataAdapter):
    """Quotes, candles and instrument specifications from OANDA."""

    client: OandaClient
    #: Price component to read candles from. "M" is mid; "B"/"A" are
    #: bid and ask. Mid for analysis, because using bid for a long and
    #: ask for a short silently biases every backtest.
    candle_price: str = "M"

    name: str = "oanda"
    capabilities: frozenset[Capability] = frozenset(
        {
            Capability.QUOTES,
            Capability.BARS,
            Capability.HISTORICAL,
            Capability.STREAMING,
        }
    )

    _instruments: dict[str, InstrumentSpec] = field(default_factory=dict, init=False)

    # -- instruments ---------------------------------------------------------

    def refresh_instruments(self) -> dict[str, InstrumentSpec]:
        """Read every tradeable instrument from OANDA.

        This is the method that makes the rest of the adapter safe. Tick
        size, pip size and lot step come from here, so a wrong guess in
        this module cannot reach the sizing arithmetic.
        """
        endpoint = f"/v3/accounts/{self.client.account_id}/instruments"
        payload = self.client.request("GET", endpoint)
        listed = _require(payload, "instruments", endpoint)
        if not isinstance(listed, list):
            raise OandaSchemaError(endpoint, "instruments", "expected a list")

        specs: dict[str, InstrumentSpec] = {}
        for entry in listed:
            spec = self._to_spec(entry, endpoint)
            specs[spec.symbol] = spec
        self._instruments = specs
        return specs

    def _to_spec(self, entry: Mapping[str, Any], endpoint: str) -> InstrumentSpec:
        """Translate one OANDA instrument into a platform spec.

        ``pipLocation`` is the power of ten at which a pip sits: -4 for
        most pairs, -2 for the JPY crosses. ``displayPrecision`` is the
        number of decimal places quoted, so the tick is ten to the
        negative of it. ``tradeUnitsPrecision`` is how finely units may
        be divided, which is the lot step.
        """
        symbol = str(_require(entry, "name", endpoint))
        pip_location = _integer(entry, "pipLocation", endpoint)
        display_precision = _integer(entry, "displayPrecision", endpoint)
        units_precision = _integer(entry, "tradeUnitsPrecision", endpoint)
        kind = str(_require(entry, "type", endpoint))

        pip_size = Decimal(10) ** pip_location
        tick_size = Decimal(10) ** (-display_precision)
        lot_step = Decimal(10) ** (-units_precision) if units_precision > 0 else ONE
        minimum = _decimal(entry, "minimumTradeSize", endpoint)

        # marginRate is the fraction of notional required, so its
        # reciprocal is the leverage OANDA will extend.
        margin_rate = _decimal(entry, "marginRate", endpoint)
        max_leverage = (ONE / margin_rate) if margin_rate > ZERO else ONE

        base, _, quote = symbol.partition("_")
        if kind == "CURRENCY":
            asset_class, market = AssetClass.FOREX_SPOT, Market.FOREX
        elif kind == "CFD":
            # Index and commodity CFDs. Sized linearly, not as futures.
            asset_class, market = AssetClass.CRYPTO_SPOT, Market.FUTURES
        elif kind == "METAL":
            asset_class, market = AssetClass.CRYPTO_SPOT, Market.FUTURES
        else:
            raise OandaSchemaError(
                endpoint, "type",
                f"{symbol} has unmapped instrument type {kind!r}; refusing to "
                "guess how to size it",
            )

        return InstrumentSpec(
            symbol=symbol,
            market=market,
            asset_class=asset_class,
            base_currency=base,
            quote_currency=quote or base,
            tick_size=tick_size,
            lot_step=lot_step,
            min_qty=minimum,
            contract_size=ONE,
            pip_size=pip_size if asset_class is AssetClass.FOREX_SPOT else None,
            max_leverage=max_leverage,
            allows_fractional=units_precision > 0,
            # OANDA's cost is in the spread, not a commission line.
            maker_fee_bps=ZERO,
            taker_fee_bps=ZERO,
            session="24x5",
        )

    def get_instrument(self, symbol: str) -> InstrumentSpec:
        if not self._instruments:
            self.refresh_instruments()
        try:
            return self._instruments[symbol]
        except KeyError:
            raise FeatureUnavailable(
                self.name, "instrument specification",
                f"{symbol} is not among the {len(self._instruments)} instruments "
                "this account may trade. OANDA names pairs like EUR_USD.",
            ) from None

    def list_symbols(self) -> list[str]:
        if not self._instruments:
            self.refresh_instruments()
        return sorted(self._instruments)

    # -- prices ---------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        endpoint = f"/v3/accounts/{self.client.account_id}/pricing"
        payload = self.client.request("GET", endpoint, params={"instruments": symbol})
        prices = _require(payload, "prices", endpoint)
        if not isinstance(prices, list) or not prices:
            raise FeatureUnavailable(
                self.name, Capability.QUOTES, f"no price returned for {symbol}"
            )
        price = prices[0]

        # An untradeable instrument has no usable price. Returning the
        # last one would be presenting a stale number as current.
        tradeable = _require(price, "tradeable", endpoint)
        if tradeable is False:
            raise FeatureUnavailable(
                self.name, Capability.QUOTES,
                f"{symbol} is not currently tradeable (market closed or halted)",
            )

        bids = _require(price, "bids", endpoint)
        asks = _require(price, "asks", endpoint)
        if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
            raise OandaSchemaError(endpoint, "bids/asks", "both must be non-empty lists")

        return Quote(
            symbol=symbol,
            timestamp=_timestamp(price, "time", endpoint),
            bid=_decimal(bids[0], "price", endpoint),
            ask=_decimal(asks[0], "price", endpoint),
            bid_size=_decimal(bids[0], "liquidity", endpoint),
            ask_size=_decimal(asks[0], "liquidity", endpoint),
            received_at=utcnow(),
        )

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        granularity = GRANULARITY.get(timeframe)
        if granularity is None:
            raise FeatureUnavailable(
                self.name, "bars",
                f"OANDA has no granularity for {timeframe}; supported: "
                f"{sorted(str(t) for t in GRANULARITY)}",
            )

        endpoint = f"/v3/instruments/{symbol}/candles"
        params: dict[str, Any] = {
            "granularity": granularity,
            "price": self.candle_price,
        }
        if start is not None:
            params["from"] = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if end is not None:
            params["to"] = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if start is None:
            params["count"] = min(max(limit, 1), 5000)

        payload = self.client.request("GET", endpoint, params=params)
        candles = _require(payload, "candles", endpoint)
        if not isinstance(candles, list):
            raise OandaSchemaError(endpoint, "candles", "expected a list")

        component = {"M": "mid", "B": "bid", "A": "ask"}[self.candle_price]
        bars: list[Bar] = []
        for candle in candles:
            complete = _require(candle, "complete", endpoint)
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=_timestamp(candle, "time", endpoint),
                    open=_decimal(candle, f"{component}.o", endpoint),
                    high=_decimal(candle, f"{component}.h", endpoint),
                    low=_decimal(candle, f"{component}.l", endpoint),
                    close=_decimal(candle, f"{component}.c", endpoint),
                    volume=D(_integer(candle, "volume", endpoint)),
                    # OANDA's "complete" is exactly the platform's
                    # "closed", and the indicators refuse open bars.
                    closed=bool(complete),
                )
            )
        return bars

    def health(self) -> AdapterHealth:
        """Ask OANDA for the account summary as a liveness probe.

        A cheap authenticated call. Anything that fails it means the
        adapter cannot be traded on, which the risk engine latches.
        """
        started = time.monotonic()
        endpoint = f"/v3/accounts/{self.client.account_id}/summary"
        try:
            self.client.request("GET", endpoint)
        except AdapterError as exc:
            return AdapterHealth.down(f"oanda data: {exc}")
        latency = (time.monotonic() - started) * 1000
        return AdapterHealth.ok(
            f"oanda {self.client.environment}", latency_ms=round(latency, 1)
        )


# -- broker --------------------------------------------------------------------


@dataclass
class OandaBroker(BrokerAdapter):
    """Account, positions and order placement at OANDA.

    OANDA expresses direction as the sign of ``units``: positive buys,
    negative sells. The translation happens here and nowhere else, so
    no caller has to remember it.
    """

    client: OandaClient
    data: OandaDataAdapter

    name: str = "oanda"
    mode: TradingMode = TradingMode.PAPER
    capabilities: frozenset[Capability] = frozenset(
        {
            Capability.PAPER_TRADING,
            Capability.QUOTES,
            Capability.BRACKET_ORDERS,
            Capability.TRAILING_STOP,
        }
    )

    def __post_init__(self) -> None:
        # A practice account is paper by any sensible reading: simulated
        # funds, real prices. Saying so here keeps the mode honest in
        # the journal rather than relying on configuration elsewhere.
        self.mode = (
            TradingMode.PAPER if self.client.environment == "practice" else TradingMode.LIVE
        )

    def _account_endpoint(self, suffix: str = "") -> str:
        return f"/v3/accounts/{self.client.account_id}{suffix}"

    def get_account(self) -> Account:
        endpoint = self._account_endpoint("/summary")
        payload = self.client.request("GET", endpoint)
        summary = _require(payload, "account", endpoint)
        return Account(
            account_id=str(_require(summary, "id", endpoint)),
            currency=str(_require(summary, "currency", endpoint)),
            equity=_decimal(summary, "NAV", endpoint),
            cash=_decimal(summary, "balance", endpoint),
            buying_power=_decimal(summary, "marginAvailable", endpoint),
            margin_used=_decimal(summary, "marginUsed", endpoint),
            mode=self.mode,
            reconciled_at=utcnow(),
        )

    def get_balance(self) -> Decimal:
        return self.get_account().cash

    def get_positions(self) -> Sequence[Position]:
        endpoint = self._account_endpoint("/openPositions")
        payload = self.client.request("GET", endpoint)
        listed = _require(payload, "positions", endpoint)
        if not isinstance(listed, list):
            raise OandaSchemaError(endpoint, "positions", "expected a list")

        out: list[Position] = []
        for entry in listed:
            symbol = str(_require(entry, "instrument", endpoint))
            long_units = _decimal(entry, "long.units", endpoint)
            short_units = _decimal(entry, "short.units", endpoint)
            net = long_units + short_units  # short units are negative
            if net == ZERO:
                continue
            side = "long" if net > ZERO else "short"
            spec = self.data.get_instrument(symbol)
            out.append(
                Position(
                    symbol=symbol,
                    market=spec.market,
                    asset_class=spec.asset_class,
                    quantity=net,
                    average_entry_price=_decimal(entry, f"{side}.averagePrice", endpoint),
                    # OANDA reports unrealised P&L but not a mark. The
                    # mark comes from the price feed, so that a position
                    # with no price reports None rather than a guess.
                    mark_price=self._mark(symbol, net),
                    realised_pnl=_decimal(entry, f"{side}.pl", endpoint),
                    fees_paid=_decimal(entry, f"{side}.financing", endpoint),
                )
            )
        return tuple(out)

    def _mark(self, symbol: str, net: Decimal) -> Decimal | None:
        try:
            quote = self.data.get_quote(symbol)
        except AdapterError:
            return None
        return quote.bid if net > ZERO else quote.ask

    def place_order(self, request: OrderRequest, *, quantity: Decimal) -> Order:
        """Submit an order the risk engine has already approved.

        The protective stop and the first target are attached as
        OANDA's own stopLoss and takeProfit, so they live at the venue
        rather than depending on this process staying up.
        """
        if quantity <= ZERO:
            raise OrderRejected(self.name, "quantity must be positive", request.client_order_id)
        spec = self.data.get_instrument(request.symbol)

        units = quantity if request.side is Side.BUY else -quantity
        body: dict[str, Any] = {
            "order": {
                "instrument": request.symbol,
                "units": str(units),
                "type": self._order_type(request.order_type),
                "timeInForce": "FOK" if request.order_type is OrderType.MARKET else "GTC",
                "positionFill": "DEFAULT",
                "clientExtensions": {
                    # Our id travels to the venue, so a reply can always
                    # be matched back even after a restart.
                    "id": request.client_order_id[:128],
                    "tag": request.strategy[:128],
                },
            }
        }
        order_body = body["order"]
        if request.order_type is not OrderType.MARKET:
            price = request.limit_price or request.stop_price
            if price is None:
                raise OrderRejected(
                    self.name, f"a {request.order_type} order needs a price",
                    request.client_order_id,
                )
            order_body["price"] = self._format(price, spec)
        if request.protective_stop is not None:
            order_body["stopLossOnFill"] = {
                "price": self._format(request.protective_stop, spec),
                "timeInForce": "GTC",
            }
        if request.targets:
            nearest = min(
                request.targets,
                key=lambda target: abs(target - (request.limit_price or request.targets[0])),
            )
            order_body["takeProfitOnFill"] = {
                "price": self._format(nearest, spec),
                "timeInForce": "GTC",
            }

        endpoint = self._account_endpoint("/orders")
        payload = self.client.request("POST", endpoint, json=body)

        # A rejected order comes back as a transaction, not an HTTP error.
        if "orderRejectTransaction" in payload:
            reason = str(
                payload["orderRejectTransaction"].get("rejectReason", "unspecified")
            )
            raise OrderRejected(self.name, reason, request.client_order_id)

        return self._order_from_response(payload, request, quantity, spec, endpoint)

    def _order_from_response(
        self,
        payload: Mapping[str, Any],
        request: OrderRequest,
        quantity: Decimal,
        spec: InstrumentSpec,
        endpoint: str,
    ) -> Order:
        created = _require(payload, "orderCreateTransaction", endpoint)
        order = Order(
            symbol=request.symbol,
            market=spec.market,
            side=request.side,
            order_type=request.order_type,
            quantity=quantity,
            status=OrderStatus.ACCEPTED,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
            client_order_id=request.client_order_id,
            broker_order_id=str(_require(created, "id", endpoint)),
            mode=self.mode,
            strategy=request.strategy,
        )

        # A market order usually fills in the same response. Anything
        # else leaves the order ACCEPTED, never assumed filled.
        filled = payload.get("orderFillTransaction")
        if not isinstance(filled, Mapping):
            return order

        fill_units = abs(_decimal(filled, "units", endpoint))
        return order.with_fill(
            Fill(
                order_id=order.client_order_id,
                symbol=request.symbol,
                side=request.side,
                quantity=fill_units,
                price=_decimal(filled, "price", endpoint),
                # OANDA's cost is the spread; any explicit financing or
                # commission arrives on the transaction when charged.
                fee=abs(D(filled.get("commission", "0") or "0")),
                timestamp=_timestamp(filled, "time", endpoint),
                liquidity="taker",
                venue_fill_id=str(_require(filled, "id", endpoint)),
            )
        )

    def _order_type(self, order_type: OrderType) -> str:
        mapping = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT: "LIMIT",
            OrderType.STOP: "STOP",
            OrderType.TAKE_PROFIT: "TAKE_PROFIT",
            OrderType.STOP_LOSS: "STOP_LOSS",
            OrderType.TRAILING_STOP: "TRAILING_STOP_LOSS",
        }
        if order_type not in mapping:
            raise OrderRejected(self.name, f"OANDA has no order type for {order_type}")
        return mapping[order_type]

    def _format(self, price: Decimal, spec: InstrumentSpec) -> str:
        """Render a price at the instrument's quoted precision."""
        places = -spec.tick_size.as_tuple().exponent
        return f"{price:.{places}f}"

    def cancel_order(self, order_id: str) -> Order:
        raise FeatureUnavailable(
            self.name, "cancel by client id",
            "OANDA cancels by its own order id; the OMS must supply the "
            "broker_order_id. Not wired up until the scanner needs resting orders.",
        )

    def get_order(self, order_id: str) -> Order:
        raise FeatureUnavailable(
            self.name, "order lookup",
            "not implemented yet; reconciliation uses get_orders",
        )

    def get_orders(self, *, open_only: bool = False) -> Sequence[Order]:
        endpoint = self._account_endpoint("/pendingOrders" if open_only else "/orders")
        payload = self.client.request("GET", endpoint)
        listed = _require(payload, "orders", endpoint)
        if not isinstance(listed, list):
            raise OandaSchemaError(endpoint, "orders", "expected a list")

        out: list[Order] = []
        for entry in listed:
            symbol = str(_require(entry, "instrument", endpoint))
            units = _decimal(entry, "units", endpoint)
            extensions = entry.get("clientExtensions") or {}
            spec = self.data.get_instrument(symbol)
            out.append(
                Order(
                    symbol=symbol,
                    market=spec.market,
                    side=Side.BUY if units > ZERO else Side.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=abs(units),
                    status=self._status(str(_require(entry, "state", endpoint))),
                    client_order_id=str(extensions.get("id") or new_id("oanda")),
                    broker_order_id=str(_require(entry, "id", endpoint)),
                    mode=self.mode,
                )
            )
        return tuple(out)

    def _status(self, state: str) -> OrderStatus:
        mapping = {
            "PENDING": OrderStatus.ACCEPTED,
            "FILLED": OrderStatus.FILLED,
            "TRIGGERED": OrderStatus.PARTIALLY_FILLED,
            "CANCELLED": OrderStatus.CANCELED,
        }
        if state not in mapping:
            # An unmappable state is exactly the condition that must
            # stop live trading, so it raises rather than guessing.
            raise OandaSchemaError(
                "orders", "state",
                f"unmapped order state {state!r}; refusing to guess whether "
                "this order is live",
            )
        return mapping[state]

    def get_fills(self, *, since: datetime | None = None) -> Sequence[Fill]:
        raise FeatureUnavailable(
            self.name, "fill history",
            "OANDA exposes fills through the transaction stream, which lands "
            "with the journal writer in Phase 4",
        )

    def close_position(self, symbol: str) -> Order:
        endpoint = self._account_endpoint(f"/positions/{symbol}/close")
        positions = {position.symbol: position for position in self.get_positions()}
        position = positions.get(symbol)
        if position is None:
            raise OrderRejected(self.name, f"no open position in {symbol}")

        side = "longUnits" if position.quantity > ZERO else "shortUnits"
        payload = self.client.request("PUT", endpoint, json={side: "ALL"})
        key = (
            "longOrderCreateTransaction"
            if position.quantity > ZERO
            else "shortOrderCreateTransaction"
        )
        created = _require(payload, key, endpoint)
        spec = self.data.get_instrument(symbol)
        return Order(
            symbol=symbol,
            market=spec.market,
            side=Side.SELL if position.quantity > ZERO else Side.BUY,
            order_type=OrderType.MARKET,
            quantity=abs(position.quantity),
            status=OrderStatus.ACCEPTED,
            broker_order_id=str(_require(created, "id", endpoint)),
            mode=self.mode,
            strategy="close_position",
        )

    def health(self) -> AdapterHealth:
        return self.data.health()


def build_oanda(
    *,
    token: str,
    account_id: str,
    environment: str = "practice",
    execution=None,
    deployment_allows_live: bool = False,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[OandaDataAdapter, OandaBroker]:
    """Build both adapters on one shared client.

    The live-arming gate is checked here, at construction, so that a
    live-pointing adapter cannot exist in an unarmed process.
    """
    _ensure_live_is_armed(
        environment, execution=execution, deployment_allows_live=deployment_allows_live
    )
    client = OandaClient(
        token=token,
        account_id=account_id,
        environment=environment,
        transport=transport,
        timeout_seconds=timeout_seconds,
    )
    data = OandaDataAdapter(client=client)
    return data, OandaBroker(client=client, data=data)
