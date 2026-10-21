import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agentguard.agents import AgentLoadError, resolve_agent
from agentguard.cli import main
from agentguard.ledger import IdempotencyConflict, InMemoryLedger, InsufficientFunds
from agentguard.money import format_cents, parse_money
from agentguard.runner import run_scenario
from agentguard.scenario import ScenarioError, parse_scenario, resolve_scenario

BASE = {
    "scenario": {"id": "t", "name": "t"},
    "task": {"prompt": "p"},
    "catalog": [{"sku": "a", "name": "A", "price": "1.00"}],
    "check": [{"type": "within_budget"}],
}


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MoneyTests(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(parse_money("$12.99"), 1299)
        self.assertEqual(parse_money(50), 5000)
        self.assertEqual(format_cents(1299), "$12.99")

    def test_rejects_bad_values(self):
        for bad in ("-1", "abc", "1.005", "nan"):
            with self.assertRaises(ValueError):
                parse_money(bad)


class LedgerTests(unittest.TestCase):
    def test_idempotent_charge_applies_once(self):
        ledger = InMemoryLedger(funds_cents=1000)
        a = ledger.charge(destination="m", amount_cents=300, idempotency_key="k")
        b = ledger.charge(destination="m", amount_cents=300, idempotency_key="k")
        self.assertEqual(a, b)
        self.assertEqual(ledger.balance("m"), 300)
        self.assertEqual(len(ledger.transactions()), 1)

    def test_key_reuse_with_different_amount_conflicts(self):
        ledger = InMemoryLedger(funds_cents=1000)
        ledger.charge(destination="m", amount_cents=300, idempotency_key="k")
        with self.assertRaises(IdempotencyConflict):
            ledger.charge(destination="m", amount_cents=400, idempotency_key="k")

    def test_insufficient_funds(self):
        with self.assertRaises(InsufficientFunds):
            InMemoryLedger(funds_cents=100).charge(destination="m", amount_cents=101)


class ScenarioTests(unittest.TestCase):
    def test_builtin_loads(self):
        s = resolve_scenario("duplicate-after-timeout")
        self.assertEqual(s.default_budget_cents, 5000)
        self.assertEqual(s.faults[0].type, "timeout_after_charge")

    def test_minimal_valid(self):
        self.assertEqual(parse_scenario(BASE).id, "t")

    def test_validation_errors(self):
        cases = {
            "unknown check": {**BASE, "check": [{"type": "nope"}]},
            "unknown fault": {**BASE, "fault": [{"type": "nope"}]},
            "no checks": {k: v for k, v in BASE.items() if k != "check"},
            "no catalog": {**BASE, "catalog": []},
            "bad price": {**BASE, "catalog": [{"sku": "a", "name": "A", "price": "-3"}]},
            "bad severity": {**BASE, "check": [{"type": "within_budget", "severity": "x"}]},
            "bad on_call": {**BASE, "fault": [{"type": "timeout_after_charge", "on_call": 0}]},
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(ScenarioError):
                parse_scenario(data)

    def test_unknown_scenario(self):
        with self.assertRaises(ScenarioError):
            resolve_scenario("does-not-exist")


class EndToEndTests(unittest.TestCase):
    def run_agent(self, name, budget=None):
        return run_scenario(resolve_scenario("duplicate-after-timeout"), resolve_agent(name), name, budget)

    def test_naive_agent_double_charges(self):
        report = self.run_agent("naive")
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.spent_cents, 2598)
        self.assertEqual([c.type for c in report.failed], ["no_duplicate_charges"])

    def test_careful_agent_passes(self):
        report = self.run_agent("careful")
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.spent_cents, 1299)

    def test_budget_override_trips_budget_check(self):
        report = self.run_agent("naive", budget=2000)
        self.assertIn("within_budget", [c.type for c in report.failed])

    def test_crashing_agent_is_reported_not_raised(self):
        class Boom:
            def run(self, task, tools):
                raise RuntimeError("kaboom")

        report = run_scenario(resolve_scenario("duplicate-after-timeout"), Boom(), "boom")
        self.assertEqual(report.exit_code, 2)
        self.assertIn("kaboom", report.agent_error)

    def test_trace_records_tool_calls(self):
        report = self.run_agent("naive")
        tools = [e["tool"] for e in report.trace]
        self.assertEqual(tools, ["checkout", "checkout"])
        self.assertIn("MerchantTimeout", report.trace[0]["error"])


class BudgetScenarioTests(unittest.TestCase):
    def test_naive_overruns_careful_does_not(self):
        s = resolve_scenario("budget-overrun-cart")
        naive = run_scenario(s, resolve_agent("naive"), "naive")
        careful = run_scenario(s, resolve_agent("careful"), "careful")
        self.assertEqual([c.type for c in naive.failed], ["within_budget"])
        self.assertEqual(naive.spent_cents, 7497)
        self.assertEqual(careful.exit_code, 0)
        self.assertLessEqual(careful.spent_cents, 5000)

    def test_ledger_flag(self):
        args = ("test", "--agent", "careful", "--scenario", "budget-overrun-cart")
        self.assertEqual(cli(*args, "--ledger", "agentguard.ledger:in_memory_ledger")[0], 0)
        code, _, err = cli(*args, "--ledger", "nope:x")
        self.assertEqual((code, "cannot load ledger" in err), (2, True))


class M3ScenarioTests(unittest.TestCase):
    def outcome(self, scenario, agent):
        return run_scenario(resolve_scenario(scenario), resolve_agent(agent), agent)

    def test_prompt_injection(self):
        naive = self.outcome("merchant-prompt-injection", "naive")
        self.assertEqual([c.type for c in naive.failed], ["only_allowed_skus"])
        self.assertEqual(naive.spent_cents, 1299 + 3999)
        careful = self.outcome("merchant-prompt-injection", "careful")
        self.assertEqual((careful.exit_code, careful.spent_cents), (0, 1299))

    def test_missing_confirmation(self):
        naive = self.outcome("missing-confirmation", "naive")
        self.assertEqual([c.type for c in naive.failed], ["confirmation_before_charge"])
        careful = self.outcome("missing-confirmation", "careful")
        self.assertEqual((careful.exit_code, careful.spent_cents), (0, 0))

    def test_approved_purchase_after_confirmation_passes(self):
        import dataclasses
        s = resolve_scenario("missing-confirmation")
        s = dataclasses.replace(s, task_params={**s.task_params, "user_approves": True})
        report = run_scenario(s, resolve_agent("careful"), "careful")
        self.assertEqual((report.exit_code, report.spent_cents), (0, 89900))


class AgentResolutionTests(unittest.TestCase):
    def test_module_attr(self):
        self.assertTrue(hasattr(resolve_agent("agentguard.agents:CarefulAgent"), "run"))

    def test_bad_specs(self):
        for spec in ("nope", "no.such.module:X", "agentguard.agents:Missing"):
            with self.subTest(spec), self.assertRaises(AgentLoadError):
                resolve_agent(spec)


class CliTests(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(cli("test", "--agent", "careful", "--scenario", "duplicate-after-timeout")[0], 0)
        self.assertEqual(cli("test", "--agent", "naive", "--scenario", "duplicate-after-timeout")[0], 1)
        code, _, err = cli("test", "--agent", "naive", "--scenario", "missing")
        self.assertEqual(code, 2)
        self.assertIn("unknown scenario", err)

    def test_json_output(self):
        code, out, _ = cli("test", "--agent", "naive", "--scenario", "duplicate-after-timeout", "--json", "--budget", "20")
        data = json.loads(out)
        self.assertEqual((code, data["budget_cents"], data["passed"]), (1, 2000, False))

    def test_bad_budget_is_usage_error(self):
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            main(["test", "--agent", "naive", "--scenario", "duplicate-after-timeout", "--budget", "-5"])
        self.assertEqual(cm.exception.code, 2)

    def test_list_and_validate(self):
        self.assertIn("duplicate-after-timeout", cli("list")[1])
        self.assertEqual(cli("validate", "src/agentguard/scenarios/duplicate-after-timeout.toml")[0], 0)


if __name__ == "__main__":
    unittest.main()
