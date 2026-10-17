"""Agent interface, two reference agents, and agent resolution for `--agent`."""

from __future__ import annotations

import importlib
import inspect
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agentguard.sandbox import MerchantTimeout, Toolbox


@dataclass(frozen=True)
class Task:
    prompt: str
    budget_cents: int
    params: dict[str, Any]


@dataclass
class AgentResult:
    status: str  # "success" | "failure" - what the agent claims happened
    message: str = ""


class Agent(Protocol):
    def run(self, task: Task, tools: Toolbox) -> AgentResult: ...


def _items(task: Task) -> list[dict[str, Any]]:
    if "items" in task.params:
        return list(task.params["items"])
    return [{"sku": task.params["sku"], "quantity": task.params.get("quantity", 1)}]


class NaiveAgent:
    """Buys everything asked for, ignores the budget, and retries timeouts without checking state."""

    def run(self, task: Task, tools: Toolbox) -> AgentResult:
        bought = []
        for item in _items(task):
            for _ in range(3):
                try:
                    order = tools.checkout(item["sku"], item["quantity"])
                    bought.append(order["order_id"])
                    break
                except MerchantTimeout:
                    continue
        return AgentResult("success" if bought else "failure", f"orders: {', '.join(bought) or 'none'}")


class CarefulAgent:
    """Stays within budget, reuses one idempotency key per item, and checks history before retrying."""

    def run(self, task: Task, tools: Toolbox) -> AgentResult:
        remaining = task.budget_cents - sum(o["total_cents"] for o in tools.order_history())
        bought, skipped = [], []
        for item in _items(task):
            sku, qty = item["sku"], item["quantity"]
            found = [r for r in tools.search(sku) if r["sku"] == sku]
            if not found or found[0]["price_cents"] * qty > remaining:
                skipped.append(sku)
                continue
            key = f"agentguard-{uuid.uuid4()}"
            for _ in range(3):
                try:
                    order = tools.checkout(sku, qty, idempotency_key=key)
                except MerchantTimeout:
                    prior = [o for o in tools.order_history() if o.get("sku") == sku]
                    if not prior:
                        continue
                    order = prior[0]
                remaining -= order["total_cents"]
                bought.append(order["order_id"])
                break
        msg = f"orders: {', '.join(bought) or 'none'}"
        return AgentResult("success" if bought else "failure", msg + (f"; skipped over budget: {', '.join(skipped)}" if skipped else ""))


BUILTIN_AGENTS: dict[str, Callable[[], Agent]] = {"naive": NaiveAgent, "careful": CarefulAgent}


class _FunctionAgent:
    def __init__(self, fn: Callable[[Task, Toolbox], AgentResult]) -> None:
        self._fn = fn

    def run(self, task: Task, tools: Toolbox) -> AgentResult:
        return self._fn(task, tools)


class AgentLoadError(ValueError):
    pass


def resolve_agent(spec: str) -> Agent:
    """`spec` is a built-in name or `module:attr` where attr is an Agent class/instance or a function."""
    if spec in BUILTIN_AGENTS:
        return BUILTIN_AGENTS[spec]()
    module_name, sep, attr = spec.partition(":")
    if not sep or not attr:
        raise AgentLoadError(
            f"unknown agent {spec!r}: use a built-in ({', '.join(BUILTIN_AGENTS)}) or 'module:attr'"
        )
    try:
        obj = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise AgentLoadError(f"cannot load agent {spec!r}: {exc}") from None
    if inspect.isclass(obj):
        obj = obj()
    if hasattr(obj, "run"):
        return obj
    if callable(obj):
        return _FunctionAgent(obj)
    raise AgentLoadError(f"{spec!r} is neither an Agent nor a callable")
