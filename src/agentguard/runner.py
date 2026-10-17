"""Run one agent against one scenario in a fresh sandbox and produce a report."""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from agentguard.agents import Agent, AgentResult, Task
from agentguard.checks import CHECKS, RunContext
from agentguard.ledger import LedgerBackend, in_memory_ledger
from agentguard.money import format_cents
from agentguard.sandbox import Merchant, Toolbox, Trace
from agentguard.scenario import Scenario, ScenarioError


@dataclass(frozen=True)
class CheckOutcome:
    type: str
    severity: str
    passed: bool
    message: str


@dataclass
class Report:
    scenario_id: str
    agent: str
    budget_cents: int
    spent_cents: int
    agent_status: str | None
    agent_message: str
    agent_error: str | None
    checks: list[CheckOutcome]
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckOutcome]:
        return [c for c in self.checks if not c.passed and c.severity == "fail"]

    @property
    def warned(self) -> list[CheckOutcome]:
        return [c for c in self.checks if not c.passed and c.severity == "warn"]

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 1
        return 2 if self.agent_error else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "agent": self.agent,
            "budget_cents": self.budget_cents,
            "spent_cents": self.spent_cents,
            "passed": self.exit_code == 0,
            "agent_status": self.agent_status,
            "agent_message": self.agent_message,
            "agent_error": self.agent_error,
            "checks": [c.__dict__ for c in self.checks],
            "trace": self.trace,
        }

    def render(self) -> str:
        lines = [
            f"scenario: {self.scenario_id}   agent: {self.agent}   budget: {format_cents(self.budget_cents)}",
            f"agent said: {self.agent_status or 'n/a'}" + (f" - {self.agent_message}" if self.agent_message else ""),
        ]
        if self.agent_error:
            lines.append(f"agent crashed:\n{self.agent_error.rstrip()}")
        for c in self.checks:
            label = "PASS" if c.passed else ("FAIL" if c.severity == "fail" else "WARN")
            lines.append(f"  [{label}] {c.type}: {c.message}")
        verdict = "FAIL" if self.failed else ("ERROR" if self.agent_error else "PASS")
        lines.append(f"result: {verdict} ({len(self.failed)} failed, {len(self.warned)} warnings)")
        return "\n".join(lines)


def run_scenario(
    scenario: Scenario,
    agent: Agent,
    agent_name: str,
    budget_cents: int | None = None,
    ledger_factory: Callable[[int], LedgerBackend] | None = None,
) -> Report:
    budget = budget_cents if budget_cents is not None else scenario.default_budget_cents
    if budget is None:
        raise ScenarioError(f"scenario {scenario.id!r} has no default_budget; pass --budget")
    ledger = (ledger_factory or in_memory_ledger)(scenario.wallet_funds_cents)
    trace = Trace()
    tools = Toolbox(Merchant(scenario, ledger), trace)
    task = Task(prompt=scenario.prompt, budget_cents=budget, params=dict(scenario.task_params))

    result: AgentResult | None = None
    error: str | None = None
    try:
        result = agent.run(task, tools)
    except Exception:
        error = traceback.format_exc()

    txns = ledger.transactions()
    ctx = RunContext(budget_cents=budget, transactions=txns, trace=trace, result=result)
    outcomes = []
    for spec in scenario.checks:
        verdict = CHECKS[spec.type](ctx, spec.params)
        outcomes.append(CheckOutcome(spec.type, spec.severity, verdict.passed, verdict.message))
    return Report(
        scenario_id=scenario.id,
        agent=agent_name,
        budget_cents=budget,
        spent_cents=sum(t.amount_cents for t in txns),
        agent_status=result.status if result else None,
        agent_message=result.message if result else "",
        agent_error=error,
        checks=outcomes,
        trace=[e.__dict__ for e in trace.events],
    )
