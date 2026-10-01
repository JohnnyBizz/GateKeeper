"""Order management — specification section 18.

An order's status is a fact about the venue, not about our intentions.
This module encodes that in two ways.

**The state machine is explicit.** Every transition is listed, and an
illegal one raises rather than quietly overwriting. The transition that
does not exist anywhere in the table is SUBMITTED to FILLED without a
fill: an order becomes filled because a fill arrived, never because
time passed or because we sent it optimistically.

**Reconciliation is a first-class operation.** :meth:`OrderManager.reconcile`
compares what we believe against what the broker reports and returns
the differences. Any unexplained difference means the account state
cannot be reliably determined, which under specification section 42
means live trading stops until a human looks at it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence

from gtcc.domain.enums import OrderStatus, RiskAction, TradingMode
from gtcc.domain.market_data import utcnow
from gtcc.domain.money import ZERO
from gtcc.domain.orders import Fill, Order, OrderRequest
from gtcc.risk.engine import RiskVerdict

#: The complete set of legal moves. Anything absent is a bug.
LEGAL_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.REQUESTED: frozenset({OrderStatus.VALIDATED, OrderStatus.REJECTED}),
    OrderStatus.VALIDATED: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
    OrderStatus.FILLED: frozenset({OrderStatus.CLOSED}),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CLOSED: frozenset(),
}


class IllegalTransition(RuntimeError):
    def __init__(self, order_id: str, current: OrderStatus, requested: OrderStatus) -> None:
        self.order_id = order_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"{order_id}: {current} cannot become {requested}. "
            f"Legal from {current}: {sorted(LEGAL_TRANSITIONS[current]) or 'nothing (terminal)'}"
        )


class RiskNotApproved(RuntimeError):
    """An order reached the OMS without an approving risk verdict.

    There is no code path that should produce this. If it fires, the
    pipeline has been bypassed and that is an incident.
    """


class DiscrepancyKind(StrEnum):
    MISSING_AT_BROKER = "MISSING_AT_BROKER"
    UNKNOWN_LOCALLY = "UNKNOWN_LOCALLY"
    STATUS_MISMATCH = "STATUS_MISMATCH"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: DiscrepancyKind
    client_order_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class Reconciliation:
    checked_at: datetime
    discrepancies: tuple[Discrepancy, ...]

    @property
    def clean(self) -> bool:
        return not self.discrepancies

    @property
    def blocks_live_trading(self) -> bool:
        """Any unexplained difference stops live execution.

        There is no "small" discrepancy: if our view of an order and the
        broker's view disagree, every position size derived from our
        view is suspect.
        """
        return bool(self.discrepancies)

    def summary(self) -> str:
        if self.clean:
            return "orders reconciled, no differences"
        return "; ".join(f"{d.kind} {d.client_order_id}: {d.detail}" for d in self.discrepancies)


@dataclass
class OrderManager:
    """Tracks orders through their lifecycle.

    Holds no venue connection of its own. The caller submits to a broker
    adapter and reports back what happened, which keeps the state
    machine testable without a network.
    """

    orders: dict[str, Order] = field(default_factory=dict)

    # -- lifecycle ------------------------------------------------------------

    def register(
        self,
        request: OrderRequest,
        verdict: RiskVerdict,
        *,
        mode: TradingMode = TradingMode.PAPER,
    ) -> Order:
        """Turn an approved request into a VALIDATED order.

        Refuses anything the risk engine did not approve, and always
        uses the engine's approved quantity rather than the requested
        one — that is the whole point of a REDUCE verdict.
        """
        if not verdict.allowed:
            raise RiskNotApproved(
                f"{request.client_order_id}: risk engine returned {verdict.action} — "
                + "; ".join(verdict.reasons)
            )
        if verdict.approved_quantity <= ZERO:
            raise RiskNotApproved(
                f"{request.client_order_id}: approved quantity is zero"
            )

        order = Order(
            symbol=request.symbol,
            market=request.market,
            side=request.side,
            order_type=request.order_type,
            quantity=verdict.approved_quantity,
            status=OrderStatus.REQUESTED,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
            client_order_id=request.client_order_id,
            mode=mode,
            strategy=request.strategy,
        )
        order = self._transition(order, OrderStatus.VALIDATED)
        self.orders[order.client_order_id] = order
        return order

    def mark_submitted(self, client_order_id: str) -> Order:
        return self._store(
            self._transition(self._get(client_order_id), OrderStatus.SUBMITTED)
        )

    def mark_accepted(self, client_order_id: str, broker_order_id: str) -> Order:
        order = self._get(client_order_id)
        order = replace(order, broker_order_id=broker_order_id)
        return self._store(self._transition(order, OrderStatus.ACCEPTED))

    def mark_rejected(self, client_order_id: str, reason: str) -> Order:
        order = self._get(client_order_id)
        order = replace(order, reject_reason=reason)
        return self._store(self._transition(order, OrderStatus.REJECTED))

    def mark_canceled(self, client_order_id: str) -> Order:
        return self._store(self._transition(self._get(client_order_id), OrderStatus.CANCELED))

    def apply_fill(self, client_order_id: str, fill: Fill) -> Order:
        """Record a confirmed execution.

        The resulting status comes from the arithmetic — partially or
        fully filled — and is validated against the transition table
        like any other move.
        """
        order = self._get(client_order_id)
        if order.status.is_terminal:
            raise IllegalTransition(client_order_id, order.status, OrderStatus.PARTIALLY_FILLED)
        filled = order.with_fill(fill)
        self._assert_legal(order, filled.status)
        return self._store(filled)

    def close(self, client_order_id: str) -> Order:
        return self._store(self._transition(self._get(client_order_id), OrderStatus.CLOSED))

    # -- queries ---------------------------------------------------------------

    def open_orders(self) -> list[Order]:
        return [order for order in self.orders.values() if order.status.is_open]

    def get(self, client_order_id: str) -> Order | None:
        return self.orders.get(client_order_id)

    # -- reconciliation ----------------------------------------------------------

    def reconcile(
        self, broker_orders: Sequence[Order], *, now: datetime | None = None
    ) -> Reconciliation:
        """Compare our record against the broker's.

        Only orders we believe are live are checked for presence: a
        terminal order the broker has forgotten is not a discrepancy,
        it is housekeeping.
        """
        now = now or utcnow()
        by_id = {order.client_order_id: order for order in broker_orders}
        found: list[Discrepancy] = []

        for client_id, local in self.orders.items():
            remote = by_id.get(client_id)
            if remote is None:
                if local.status.is_open:
                    found.append(
                        Discrepancy(
                            kind=DiscrepancyKind.MISSING_AT_BROKER,
                            client_order_id=client_id,
                            detail=f"we hold it as {local.status}; the broker has no such order",
                        )
                    )
                continue
            if remote.status is not local.status:
                found.append(
                    Discrepancy(
                        kind=DiscrepancyKind.STATUS_MISMATCH,
                        client_order_id=client_id,
                        detail=f"we hold {local.status}, the broker reports {remote.status}",
                    )
                )
            if remote.filled_quantity != local.filled_quantity:
                found.append(
                    Discrepancy(
                        kind=DiscrepancyKind.QUANTITY_MISMATCH,
                        client_order_id=client_id,
                        detail=(
                            f"we hold {local.filled_quantity} filled, the broker reports "
                            f"{remote.filled_quantity}"
                        ),
                    )
                )

        for client_id, remote in by_id.items():
            if client_id not in self.orders and remote.status.is_open:
                found.append(
                    Discrepancy(
                        kind=DiscrepancyKind.UNKNOWN_LOCALLY,
                        client_order_id=client_id,
                        detail=f"broker holds an open {remote.status} order we never recorded",
                    )
                )

        return Reconciliation(checked_at=now, discrepancies=tuple(found))

    # -- internals ----------------------------------------------------------------

    def _get(self, client_order_id: str) -> Order:
        order = self.orders.get(client_order_id)
        if order is None:
            raise KeyError(f"no order {client_order_id!r} is being tracked")
        return order

    def _store(self, order: Order) -> Order:
        self.orders[order.client_order_id] = order
        return order

    def _assert_legal(self, order: Order, new_status: OrderStatus) -> None:
        if new_status is order.status and new_status is OrderStatus.PARTIALLY_FILLED:
            return
        if new_status not in LEGAL_TRANSITIONS[order.status]:
            raise IllegalTransition(order.client_order_id, order.status, new_status)

    def _transition(self, order: Order, new_status: OrderStatus) -> Order:
        self._assert_legal(order, new_status)
        return replace(order, status=new_status, updated_at=utcnow())
