"""Adapter failures, as types rather than strings.

The distinction that matters most: :class:`FeatureUnavailable` is not an
error condition. It is the correct, expected answer when a venue does
not publish a feed, and the caller's job is to mark that analysis
unavailable — specification section 7 — rather than to substitute a
number derived from something else.
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for anything an adapter can go wrong with."""


class FeatureUnavailable(AdapterError):
    """This venue does not offer this data or capability.

    Raised for a missing order book, absent funding rate, no open
    interest. Callers catch it and record the gap.
    """

    def __init__(self, adapter: str, feature: str, detail: str = "") -> None:
        self.adapter = adapter
        self.feature = feature
        message = f"{adapter} does not provide {feature}"
        super().__init__(f"{message}: {detail}" if detail else message)


class AuthenticationError(AdapterError):
    """Credentials missing, wrong, or lacking the needed permission."""


class RateLimited(AdapterError):
    def __init__(self, adapter: str, retry_after_seconds: float | None = None) -> None:
        self.adapter = adapter
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"{adapter} rate limited"
            + (f"; retry in {retry_after_seconds}s" if retry_after_seconds else "")
        )


class ConnectionUnhealthy(AdapterError):
    """The venue is unreachable or answering badly. Orders are refused."""


class OrderRejected(AdapterError):
    """The venue refused the order. Carries whatever reason it gave."""

    def __init__(self, adapter: str, reason: str, client_order_id: str = "") -> None:
        self.adapter = adapter
        self.reason = reason
        self.client_order_id = client_order_id
        super().__init__(f"{adapter} rejected {client_order_id or 'order'}: {reason}")


class LiveTradingDisabled(AdapterError):
    """A live order was attempted while live trading is off.

    This should be unreachable — the risk engine refuses first — so if
    it is ever raised, something bypassed the pipeline and that is a bug
    worth an alert, not a retry.
    """
