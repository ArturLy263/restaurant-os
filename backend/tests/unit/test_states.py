"""The central status rules (app.core.states) and everything kept in step
with them: model defaults and CHECK constraints, the migration's frozen
copy, the preparation event contracts and the services' status writes."""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.states import (
    BY_TABLE,
    ORDER,
    PREPARATION,
    PREPARATION_STEPS,
    IllegalTransition,
    status_check,
)
from app.db import models  # noqa: F401 - registers every model
from app.db.database import Base
from app.schemas.outbox import PREPARATION_TRANSITIONS_V1, PREPARATION_TRANSITIONS_V2

BACKEND = Path(__file__).resolve().parents[2]


def test_apply_sets_an_allowed_status_and_returns_the_previous_one():
    order = SimpleNamespace(status="OPEN")
    assert ORDER.apply(order, "SENT") == "OPEN"
    assert order.status == "SENT"


@pytest.mark.parametrize(
    ("current", "new"),
    [("CLOSED", "SENT"), ("CANCELLED", "OPEN"), ("OPEN", "CLOSED"), ("SENT", "OPEN")],
)
def test_apply_refuses_a_change_the_rules_do_not_list(current, new):
    order = SimpleNamespace(status=current)
    with pytest.raises(IllegalTransition, match=f"order: {current} -> {new}"):
        ORDER.apply(order, new)
    assert order.status == current


def test_terminal_statuses_have_no_way_out():
    for machine, terminal in [(ORDER, {"CLOSED", "CANCELLED"}), (PREPARATION, {"SERVED", "CANCELLED"})]:
        assert not {current for current, _ in machine.transitions} & terminal, machine.name


def test_preparation_rules_match_the_event_contracts():
    # Every change a preparation can make has an event version that
    # describes it: v2 is exactly the domain's set, v1 lacks discarding a
    # ready dish.
    assert PREPARATION.transitions == PREPARATION_TRANSITIONS_V2
    assert PREPARATION_TRANSITIONS_V1 == PREPARATION_TRANSITIONS_V2 - {("READY", "CANCELLED")}
    assert set(PREPARATION_STEPS.items()) <= PREPARATION.transitions


@pytest.mark.parametrize("table", sorted(BY_TABLE))
def test_models_default_to_the_initial_status_and_declare_the_check(table):
    machine = BY_TABLE[table]
    model_table = Base.metadata.tables[table]

    assert model_table.c.status.default.arg == machine.initial
    checks = {c.name: str(c.sqltext) for c in model_table.constraints if c.name == f"ck_{table}_status"}
    assert checks == {f"ck_{table}_status": status_check(machine)}


def test_migration_copy_of_the_statuses_matches_the_rules():
    path = BACKEND / "alembic" / "versions" / "f4c2a8d6b1e3_restrict_status_values.py"
    spec = importlib.util.spec_from_file_location("restrict_status_values", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert {table: set(values) for table, values in migration.STATUSES.items()} == {
        table: set(machine.statuses) for table, machine in BY_TABLE.items()
    }


def test_services_change_statuses_only_through_the_rules():
    direct = []
    for path in (BACKEND / "app" / "services").glob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.status\s*=[^=]", line):
                direct.append(f"{path.name}:{number}: {line.strip()}")
    assert direct == [], "status written without app.core.states:\n" + "\n".join(direct)
