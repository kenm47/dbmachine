"""Custom-operation registry.

Operations declared in the spec with an ``implementation: ops.<name>`` reference
a Python callable living in the project's ``ops/`` package. Because dbmachine is
installed *into the project's own uv environment*, those functions can freely
``import stripe`` / ``import pandas`` — there is no isolated-venv wall.

A custom op has the signature::

    def confirm_appointment(ctx: OpContext, *, appointment_id: str) -> Any: ...

It receives validated keyword inputs plus a transactional :class:`OpContext`
(DB connection + actions handle) and may return any JSON-serialisable value.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .actions import Actions


@dataclass
class OpContext:
    conn: object  # transactional SQLAlchemy connection
    actions: Actions
    operation: str


class OpNotFound(Exception):
    pass


def _ensure_on_path(project_root: Path) -> None:
    p = str(project_root)
    if p not in sys.path:
        sys.path.insert(0, p)


def _purge_foreign_package(top: str, project_root: Path) -> None:
    """Drop a cached top-level package if it was imported from another project.

    The CLI runs one command per process so this rarely matters, but a long-lived
    host (tests, a future server) operating several projects would otherwise reuse
    the first project's ``ops`` package for all of them.
    """
    mod = sys.modules.get(top)
    if mod is None:
        return
    file = getattr(mod, "__file__", "") or ""
    try:
        from_project = Path(file).resolve().is_relative_to(project_root.resolve())
    except (ValueError, OSError):
        from_project = False
    if not from_project:
        for name in [n for n in sys.modules if n == top or n.startswith(top + ".")]:
            del sys.modules[name]


def resolve_implementation(project_root: Path, dotted: str) -> Callable[..., Any]:
    """Resolve ``ops.confirm_appointment`` to the callable, importing from the project."""
    _ensure_on_path(project_root)
    module_name, _, attr = dotted.rpartition(".")
    _purge_foreign_package(module_name.split(".")[0], project_root)
    if not module_name:
        raise OpNotFound(f"implementation {dotted!r} must be a dotted path like 'ops.foo'")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        raise OpNotFound(
            f"could not import module {module_name!r} for operation implementation "
            f"{dotted!r}: {e}"
        ) from e
    fn = getattr(module, attr, None)
    if fn is None or not callable(fn):
        raise OpNotFound(f"{dotted!r} is not a callable in {module_name!r}")
    return fn
