"""Simulated merchant plus the tool surface an agent gets, with full call tracing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agentguard.ledger import InsufficientFunds, LedgerBackend
from agentguard.scenario import Scenario

MERCHANT_ACCOUNT = "merchant:shop"


class MerchantError(Exception):
    pass


class MerchantTimeout(MerchantError):
    """The request timed out. The caller cannot tell whether the charge happened."""


@dataclass
class Event:
    seq: int
    tool: str
    args: dict[str, Any]
    result: Any = None
    error: str | None = None


@dataclass
class Trace:
    events: list[Event] = field(default_factory=list)

    def record(self, tool: str, args: dict[str, Any]) -> Event:
        event = Event(seq=len(self.events) + 1, tool=tool, args=args)
        self.events.append(event)
        return event


class Merchant:
    def __init__(self, scenario: Scenario, ledger: LedgerBackend) -> None:
        self._catalog = {item.sku: item for item in scenario.catalog}
        self._faults = [f for f in scenario.faults if f.type == "timeout_after_charge"]
        self._ledger = ledger
        self._checkout_calls = 0

    def search(self, query: str) -> list[dict[str, Any]]:
        q = query.lower()
        return [
            {"sku": i.sku, "name": i.name, "price_cents": i.price_cents}
            for i in self._catalog.values()
            if q in i.sku.lower() or q in i.name.lower()
        ]

    def checkout(self, sku: str, quantity: int = 1, idempotency_key: str | None = None) -> dict[str, Any]:
        self._checkout_calls += 1
        call = self._checkout_calls
        item = self._catalog.get(sku)
        if item is None:
            raise MerchantError(f"unknown sku {sku!r}")
        if not isinstance(quantity, int) or quantity < 1:
            raise MerchantError("quantity must be a positive integer")
        try:
            txn = self._ledger.charge(
                destination=MERCHANT_ACCOUNT,
                amount_cents=item.price_cents * quantity,
                idempotency_key=idempotency_key,
                metadata={"sku": sku, "quantity": quantity},
            )
        except InsufficientFunds:
            raise MerchantError("payment_declined") from None
        if any(f.on_call <= call < f.on_call + f.times for f in self._faults):
            raise MerchantTimeout("checkout timed out")
        return {
            "order_id": f"order_{txn.id}",
            "sku": sku,
            "quantity": quantity,
            "total_cents": txn.amount_cents,
            "status": "confirmed",
        }

    def order_history(self) -> list[dict[str, Any]]:
        return [
            {"order_id": f"order_{t.id}", **t.metadata, "total_cents": t.amount_cents}
            for t in self._ledger.transactions()
            if t.destination == MERCHANT_ACCOUNT
        ]


class Toolbox:
    """What the agent under test calls. Every call is recorded in the trace."""

    def __init__(self, merchant: Merchant, trace: Trace) -> None:
        self._merchant = merchant
        self.trace = trace

    def _call(self, tool: str, args: dict[str, Any], fn: Callable[[], Any]) -> Any:
        event = self.trace.record(tool, args)
        try:
            event.result = fn()
        except Exception as exc:
            event.error = f"{type(exc).__name__}: {exc}"
            raise
        return event.result

    def search(self, query: str) -> list[dict[str, Any]]:
        return self._call("search", {"query": query}, lambda: self._merchant.search(query))

    def checkout(self, sku: str, quantity: int = 1, idempotency_key: str | None = None) -> dict[str, Any]:
        args = {"sku": sku, "quantity": quantity, "idempotency_key": idempotency_key}
        return self._call("checkout", args, lambda: self._merchant.checkout(sku, quantity, idempotency_key))

    def order_history(self) -> list[dict[str, Any]]:
        return self._call("order_history", {}, self._merchant.order_history)
