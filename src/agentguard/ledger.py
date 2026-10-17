"""Sandbox ledger interface.

AgentGuard talks to any backend that satisfies ``LedgerBackend``. ``InMemoryLedger`` is the
zero-dependency default; an adapter for the external ledger engine plugs in behind the same
protocol (see README, "Using your ledger engine").
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from typing import Callable, Protocol


class InsufficientFunds(Exception):
    pass


class IdempotencyConflict(Exception):
    """Same idempotency key reused for a different charge."""


@dataclass(frozen=True)
class Transaction:
    id: str
    seq: int
    source: str
    destination: str
    amount_cents: int
    idempotency_key: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class LedgerBackend(Protocol):
    def charge(
        self,
        *,
        destination: str,
        amount_cents: int,
        idempotency_key: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Transaction: ...

    def transactions(self) -> list[Transaction]: ...

    def balance(self, account: str) -> int: ...


class InMemoryLedger:
    """Single-wallet ledger with idempotent charges."""

    def __init__(self, wallet: str = "agent_wallet", funds_cents: int = 0) -> None:
        self.wallet = wallet
        self._balances: dict[str, int] = {wallet: funds_cents}
        self._txns: list[Transaction] = []
        self._by_key: dict[str, Transaction] = {}

    def charge(
        self,
        *,
        destination: str,
        amount_cents: int,
        idempotency_key: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Transaction:
        if amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        if idempotency_key is not None and idempotency_key in self._by_key:
            prior = self._by_key[idempotency_key]
            if (prior.destination, prior.amount_cents) != (destination, amount_cents):
                raise IdempotencyConflict(idempotency_key)
            return prior
        if self._balances[self.wallet] < amount_cents:
            raise InsufficientFunds(f"balance {self._balances[self.wallet]} < {amount_cents}")
        self._balances[self.wallet] -= amount_cents
        self._balances[destination] = self._balances.get(destination, 0) + amount_cents
        seq = len(self._txns) + 1
        txn = Transaction(
            id=f"txn_{seq:04d}",
            seq=seq,
            source=self.wallet,
            destination=destination,
            amount_cents=amount_cents,
            idempotency_key=idempotency_key,
            metadata=dict(metadata or {}),
        )
        self._txns.append(txn)
        if idempotency_key is not None:
            self._by_key[idempotency_key] = txn
        return txn

    def transactions(self) -> list[Transaction]:
        return list(self._txns)

    def balance(self, account: str) -> int:
        return self._balances.get(account, 0)


class LedgerLoadError(ValueError):
    pass


def load_ledger_factory(spec: str) -> Callable[[int], LedgerBackend]:
    """`module:factory`, where factory(funds_cents) returns a LedgerBackend."""
    module_name, sep, attr = spec.partition(":")
    try:
        if not sep or not attr:
            raise ValueError("expected module:factory")
        return getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError, ValueError) as exc:
        raise LedgerLoadError(f"cannot load ledger {spec!r}: {exc}") from None


def in_memory_ledger(funds_cents: int) -> InMemoryLedger:
    return InMemoryLedger(funds_cents=funds_cents)
