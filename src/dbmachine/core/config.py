"""Project configuration & discovery.

A dbmachine *project* is a directory containing ``app.dbm.yaml``. Local,
per-project state (the dynamically-chosen Postgres host port, db name,
credentials, container name) lives in ``.dbmachine/config.json`` so every
command can reach the right instance — critical because ``up`` auto-detects a
free port to dodge 5432 collisions.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIRNAME = ".dbmachine"
CONFIG_FILENAME = "config.json"
SPEC_FILENAME = "app.dbm.yaml"


@dataclass
class ProjectConfig:
    app_name: str
    db_name: str = "app"
    db_user: str = "dbm"
    db_password: str = "dbm"
    host: str = "localhost"
    port: int = 5432  # the host port chosen by `up`; persisted here
    container: str = "dbmachine_db"

    def url(self, *, driver: str = "psycopg") -> str:
        return (
            f"postgresql+{driver}://{self.db_user}:{self.db_password}"
            f"@{self.host}:{self.port}/{self.db_name}"
        )

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password}"
        )


class ProjectError(Exception):
    pass


@dataclass
class Project:
    root: Path
    config: ProjectConfig

    @property
    def spec_path(self) -> Path:
        return self.root / SPEC_FILENAME

    @property
    def config_dir(self) -> Path:
        return self.root / CONFIG_DIRNAME

    @property
    def build_dir(self) -> Path:
        return self.config_dir / "build"

    @property
    def ops_dir(self) -> Path:
        return self.root / "ops"

    @property
    def agents_md(self) -> Path:
        return self.root / "AGENTS.md"

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / CONFIG_FILENAME).write_text(
            json.dumps(asdict(self.config), indent=2) + "\n"
        )

    # -- discovery ---------------------------------------------------------

    @classmethod
    def find(cls, start: str | os.PathLike | None = None) -> "Project":
        """Walk up from ``start`` (default cwd) to locate a project root."""
        cur = Path(start or Path.cwd()).resolve()
        for d in [cur, *cur.parents]:
            cfg = d / CONFIG_DIRNAME / CONFIG_FILENAME
            if cfg.exists():
                data = json.loads(cfg.read_text())
                return cls(root=d, config=ProjectConfig(**data))
            if (d / SPEC_FILENAME).exists():
                # spec present but not yet configured (pre-`up`)
                from .spec import load_spec

                spec = load_spec(str(d / SPEC_FILENAME))
                return cls(root=d, config=ProjectConfig(app_name=spec.app.name))
        raise ProjectError(
            "no dbmachine project found here (looked for app.dbm.yaml / "
            f".dbmachine/). Run `dbmachine init` first. Searched from {cur}."
        )

    @classmethod
    def create(cls, root: str | os.PathLike, app_name: str) -> "Project":
        root_p = Path(root).resolve()
        root_p.mkdir(parents=True, exist_ok=True)
        cfg = ProjectConfig(
            app_name=app_name,
            db_name=app_name,
            container=f"dbmachine_{app_name}",
        )
        proj = cls(root=root_p, config=cfg)
        proj.save()
        return proj
