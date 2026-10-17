from __future__ import annotations

import argparse
import json
import sys

from agentguard import __version__
from agentguard.agents import AgentLoadError, resolve_agent
from agentguard.ledger import LedgerLoadError, load_ledger_factory
from agentguard.money import format_cents, parse_money
from agentguard.runner import run_scenario
from agentguard.scenario import ScenarioError, list_builtin, load_scenario_file, resolve_scenario


def _budget(text: str) -> int:
    try:
        return parse_money(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentguard", description="Safety tests for AI shopping agents.")
    parser.add_argument("--version", action="version", version=f"agentguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    test = sub.add_parser("test", help="run an agent against a scenario")
    test.add_argument("--agent", required=True, help="built-in name (naive, careful) or module:attr")
    test.add_argument("--scenario", required=True, help="built-in scenario id or path to a .toml file")
    test.add_argument("--budget", type=_budget, help="spending budget in dollars, e.g. 50 or 49.99")
    test.add_argument("--ledger", help="module:factory building a LedgerBackend from funds_cents (default: in-memory)")
    test.add_argument("--json", action="store_true", help="emit a machine-readable report")

    sub.add_parser("list", help="list built-in scenarios")

    validate = sub.add_parser("validate", help="check a scenario file")
    validate.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            for s in list_builtin():
                print(f"{s.id:28} {s.category:20} {s.name}")
            return 0
        if args.command == "validate":
            s = load_scenario_file(args.path)
            print(f"ok: {s.id} ({len(s.checks)} checks, {len(s.faults)} faults)")
            return 0
        scenario = resolve_scenario(args.scenario)
        agent = resolve_agent(args.agent)
        factory = load_ledger_factory(args.ledger) if args.ledger else None
        report = run_scenario(scenario, agent, args.agent, budget_cents=args.budget, ledger_factory=factory)
    except (ScenarioError, AgentLoadError, LedgerLoadError) as exc:
        print(f"agentguard: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
