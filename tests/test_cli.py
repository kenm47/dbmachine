"""End-to-end agent walkthrough through the real CLI.

Drives the documented contract — init → migrate → create → do → query → import
→ audit — exactly as a coding agent would, asserting the JSON envelope each
step. Runs against the embedded Postgres (CRM example, no contrib needed).
"""

import json
import os

import pytest
from typer.testing import CliRunner

from dbmachine.cli import app

pytestmark = pytest.mark.postgres
runner = CliRunner()


def _run(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code in (0, 1), result.output
    payload = json.loads(result.output)
    return payload, result


@pytest.fixture
def in_crm_project(crm_project, monkeypatch):
    proj, spec = crm_project
    monkeypatch.chdir(proj.root)
    return proj, spec


def test_full_agent_walkthrough(in_crm_project):
    proj, spec = in_crm_project

    # migrate (fresh)
    payload, _ = _run("migrate")
    assert payload["ok"] and payload["data"]["applied"]

    # inspect overview
    payload, _ = _run("inspect")
    assert payload["ok"]
    assert {e["name"] for e in payload["data"]["entities"]} == {"company", "contact", "deal"}

    # describe one entity (granular)
    payload, _ = _run("describe", "deal")
    assert payload["ok"]
    assert any(f["name"] == "stage" for f in payload["data"]["fields"])

    # create a company
    payload, _ = _run("create", "company", "--json", json.dumps({"name": "Globex"}))
    assert payload["ok"], payload
    company_id = payload["data"]["id"]

    # create a deal for it
    payload, _ = _run(
        "create", "deal", "--json",
        json.dumps({"title": "Big", "amount": 5000, "stage": "proposal", "company_id": company_id}),
    )
    assert payload["ok"], payload
    deal_id = payload["data"]["id"]

    # validation error is structured, not a crash
    payload, res = _run("create", "company", "--json", json.dumps({"name": 5, "bogus": 1}))
    assert not payload["ok"]
    assert payload["error"]["code"] == "validation"

    # run a typed operation
    payload, _ = _run("do", "win_deal", "--json", json.dumps({"deal_id": deal_id}))
    assert payload["ok"]
    assert payload["data"]["result"]["stage"] == "won"

    # read-only query against a view
    payload, _ = _run("query", "--sql", "SELECT count(*) AS n FROM company")
    assert payload["ok"]
    assert payload["data"]["rows"][0]["n"] >= 2  # seed + Globex

    # query rejects writes
    payload, _ = _run("query", "--sql", "DELETE FROM company")
    assert not payload["ok"] and payload["error"]["code"] == "query_error"

    # list with filter
    payload, _ = _run("list", "company", "--where", "name=Globex")
    assert payload["ok"] and payload["data"]["count"] == 1

    # audit log shows the writes
    payload, _ = _run("audit", "--entity", "deal")
    assert payload["ok"]
    actions = {e["action"] for e in payload["data"]["entries"]}
    assert "INSERT" in actions and "UPDATE" in actions


def test_import_dry_run_then_apply(in_crm_project, tmp_path):
    _run("migrate")
    csv = tmp_path / "companies.csv"
    csv.write_text("Name,Domain\nInitech,initech.com\nUmbrella,umbrella.com\n")

    payload, _ = _run("import", str(csv), "--entity", "company", "--dry-run")
    assert payload["ok"] and payload["data"]["dry_run"]
    assert payload["data"]["mapping"] == {"Name": "name", "Domain": "domain"}

    payload, _ = _run("import", str(csv), "--entity", "company")
    assert payload["ok"] and payload["data"]["inserted"] == 2
