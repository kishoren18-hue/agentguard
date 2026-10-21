"""Scenario format: a TOML file describing the task, the merchant, injected faults and checks."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from agentguard.money import parse_money

KNOWN_FAULTS = {"timeout_after_charge"}
SEVERITIES = {"fail", "warn"}


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogItem:
    sku: str
    name: str
    price_cents: int
    description: str = ""


@dataclass(frozen=True)
class Fault:
    type: str
    on_call: int = 1
    times: int = 1


@dataclass(frozen=True)
class CheckSpec:
    type: str
    severity: str = "fail"
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    category: str
    description: str
    prompt: str
    task_params: dict[str, object]
    default_budget_cents: int | None
    wallet_funds_cents: int
    catalog: tuple[CatalogItem, ...]
    faults: tuple[Fault, ...]
    checks: tuple[CheckSpec, ...]


def _require(table: dict, key: str, where: str) -> object:
    if key not in table:
        raise ScenarioError(f"{where}: missing required key '{key}'")
    return table[key]


def _money(table: dict, key: str, where: str) -> int:
    try:
        return parse_money(_require(table, key, where))
    except ValueError as exc:
        raise ScenarioError(f"{where}.{key}: {exc}") from None


def _positive_int(table: dict, key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ScenarioError(f"{where}.{key}: must be a positive integer")
    return value


def parse_scenario(data: dict, source: str = "<scenario>") -> Scenario:
    from agentguard.checks import CHECKS  # deferred: checks imports scenario types

    meta = _require(data, "scenario", source)
    task = _require(data, "task", source)
    if not isinstance(meta, dict) or not isinstance(task, dict):
        raise ScenarioError(f"{source}: [scenario] and [task] must be tables")

    catalog = []
    for i, raw in enumerate(data.get("catalog", [])):
        where = f"{source}: catalog[{i}]"
        catalog.append(
            CatalogItem(
                sku=str(_require(raw, "sku", where)),
                name=str(_require(raw, "name", where)),
                price_cents=_money(raw, "price", where),
                description=str(raw.get("description", "")),
            )
        )
    if not catalog:
        raise ScenarioError(f"{source}: at least one [[catalog]] item is required")
    if len({c.sku for c in catalog}) != len(catalog):
        raise ScenarioError(f"{source}: duplicate sku in catalog")

    faults = []
    for i, raw in enumerate(data.get("fault", [])):
        where = f"{source}: fault[{i}]"
        ftype = _require(raw, "type", where)
        if ftype not in KNOWN_FAULTS:
            raise ScenarioError(f"{where}: unknown fault type {ftype!r} (known: {sorted(KNOWN_FAULTS)})")
        faults.append(
            Fault(
                type=ftype,
                on_call=_positive_int(raw, "on_call", 1, where),
                times=_positive_int(raw, "times", 1, where),
            )
        )

    checks = []
    for i, raw in enumerate(data.get("check", [])):
        where = f"{source}: check[{i}]"
        ctype = _require(raw, "type", where)
        if ctype not in CHECKS:
            raise ScenarioError(f"{where}: unknown check type {ctype!r} (known: {sorted(CHECKS)})")
        severity = raw.get("severity", "fail")
        if severity not in SEVERITIES:
            raise ScenarioError(f"{where}: severity must be one of {sorted(SEVERITIES)}")
        params = {k: v for k, v in raw.items() if k not in {"type", "severity"}}
        checks.append(CheckSpec(type=ctype, severity=severity, params=params))
    if not checks:
        raise ScenarioError(f"{source}: at least one [[check]] is required")

    default_budget = _money(meta, "default_budget", f"{source}: scenario") if "default_budget" in meta else None
    task_params = {k: v for k, v in task.items() if k != "prompt"}
    return Scenario(
        id=str(_require(meta, "id", f"{source}: scenario")),
        name=str(_require(meta, "name", f"{source}: scenario")),
        category=str(meta.get("category", "uncategorized")),
        description=str(meta.get("description", "")).strip(),
        prompt=str(_require(task, "prompt", f"{source}: task")),
        task_params=task_params,
        default_budget_cents=default_budget,
        wallet_funds_cents=_money(meta, "wallet_funds", f"{source}: scenario") if "wallet_funds" in meta else 100_000,
        catalog=tuple(catalog),
        faults=tuple(faults),
        checks=tuple(checks),
    )


def load_scenario_file(path: str | Path) -> Scenario:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ScenarioError(f"cannot read {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ScenarioError(f"{path}: invalid TOML: {exc}") from None
    return parse_scenario(data, source=str(path))


def _builtin_dir():
    return resources.files("agentguard") / "scenarios"


def list_builtin() -> list[Scenario]:
    out = []
    for entry in sorted(_builtin_dir().iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".toml"):
            out.append(parse_scenario(tomllib.loads(entry.read_text()), source=entry.name))
    return out


def resolve_scenario(ref: str) -> Scenario:
    """`ref` is a path to a .toml file or the id of a built-in scenario."""
    if ref.endswith(".toml") or Path(ref).is_file():
        return load_scenario_file(ref)
    for scenario in list_builtin():
        if scenario.id == ref:
            return scenario
    known = ", ".join(s.id for s in list_builtin()) or "none"
    raise ScenarioError(f"unknown scenario {ref!r} (built-in: {known})")
