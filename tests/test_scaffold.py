"""Unit test for project scaffolding (no DB required)."""

from dbmachine.core.scaffold import init_project
from dbmachine.core.spec import load_spec


def test_init_creates_valid_project(tmp_path):
    target = tmp_path / "myapp"
    result = init_project(str(target))
    assert result["app_name"] == "myapp"

    # all expected artifacts exist
    for rel in ("app.dbm.yaml", "ops/__init__.py", "pyproject.toml",
                "docker-compose.yaml", "AGENTS.md"):
        assert (target / rel).exists(), rel
    assert (target / ".dbmachine" / "config.json").exists()

    # the scaffolded spec is valid and compiles
    spec = load_spec(str(target / "app.dbm.yaml"))
    assert spec.app.name == "myapp"
    from dbmachine.core.compiler import render_schema_sql

    assert "CREATE TABLE note" in render_schema_sql(spec)


def test_init_respects_explicit_name(tmp_path):
    result = init_project(str(tmp_path / "dir"), app_name="custom")
    assert result["app_name"] == "custom"
