"""Project scaffolding for `dbmachine init`.

Creates a project-local, ``uv``-managed directory: the spec, an ``ops/`` package
for custom operations, a generated ``docker-compose.yaml``, a project
``pyproject.toml`` (so custom ops run inside the project env where
``uv add stripe`` just works), and an initial ``AGENTS.md``.
"""

from __future__ import annotations

from pathlib import Path

from . import docker
from .config import Project
from .docs import generate_agents_md
from .spec import load_spec

STARTER_SPEC = """\
app:
  name: __APP_NAME__
  description: >
    Describe what this app is for. A coding agent reads this to understand the
    domain. Edit this spec, then run `dbmachine migrate` to apply changes.
  version: 0.1.0

# enums:
#   status:
#     description: Lifecycle state.
#     values: [active, archived]

entities:
  note:
    description: A simple note — replace with your real entities.
    fields:
      title:
        type: text
        required: true
        description: Short title of the note.
      body:
        type: text
        description: Free-form note body.
      pinned:
        type: boolean
        default: false
        description: Whether the note is pinned.

# operations:
#   archive_note:
#     description: Archive a note.
#     inputs:
#       note_id: { type: uuid, required: true }
#     implementation: ops.archive_note
#     entities: [note]

policies:
  agent:
    description: The AI agent role.
    entities:
      note: { select: true, insert: true, update: true, delete: false }
"""

STARTER_OPS = '''\
"""Custom operations for this dbmachine app.

Each operation referenced from the spec as `implementation: ops.<name>` is a
callable here. It receives an `OpContext` (transactional DB connection + actions
handle) plus validated keyword inputs, and may return any JSON-serialisable value.

Because dbmachine runs inside this project's uv environment, you can freely
`uv add <pkg>` and `import` third-party libraries here.
"""

from __future__ import annotations

import sqlalchemy as sa


# def archive_note(ctx, *, note_id: str):
#     ctx.conn.execute(
#         sa.text("UPDATE note SET pinned = false WHERE id = :id"), {"id": note_id}
#     )
#     return {"archived": note_id}
'''

PROJECT_PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"
description = "A dbmachine application."
requires-python = ">=3.10"
dependencies = [
    "dbmachine",
]

# Custom operations in ops/ run inside this environment.
# Add libraries with:  uv add <package>
"""

GITIGNORE = """\
.dbmachine/build/
.venv/
__pycache__/
*.pyc
"""


def init_project(target_dir: str, *, app_name: str | None = None) -> dict:
    root = Path(target_dir).resolve()
    name = app_name or root.name
    name = name.replace("-", "_")

    proj = Project.create(root, name)

    spec_path = proj.spec_path
    created = []
    if not spec_path.exists():
        spec_path.write_text(STARTER_SPEC.replace("__APP_NAME__", name))
        created.append(str(spec_path.relative_to(root)))

    ops_init = proj.ops_dir / "__init__.py"
    if not ops_init.exists():
        proj.ops_dir.mkdir(parents=True, exist_ok=True)
        ops_init.write_text(STARTER_OPS)
        created.append("ops/__init__.py")

    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        pyproject.write_text(PROJECT_PYPROJECT.format(name=name))
        created.append("pyproject.toml")

    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
        created.append(".gitignore")

    docker.write_compose(proj)
    created.append("docker-compose.yaml")

    # initial AGENTS.md from the starter spec
    spec = load_spec(str(spec_path))
    proj.agents_md.write_text(generate_agents_md(spec))
    created.append("AGENTS.md")

    return {
        "root": str(root),
        "app_name": name,
        "created": created,
        "next_steps": [
            "cd " + str(root),
            "uv sync            # set up the project environment",
            "dbmachine up       # start local Postgres (auto-detects a free port)",
            "dbmachine migrate  # create the schema",
            "dbmachine inspect  # see what you've got",
        ],
    }
