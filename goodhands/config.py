"""Load declarative roles and skills without executing plugin code."""

import json
import re
import tomllib
from importlib.resources import files
from pathlib import Path

from pydantic import Field

from .models import ROLES, Contract, Settings, Task


class Role(Contract):
    name: str
    version: str = "1"
    instructions: str = Field(min_length=1)
    tools: list[str]
    enabled: bool = True
    skills: list[str] = Field(default_factory=list)


PERMISSIONS = {
    "consultant": {"list_files", "read_file", "search_text"},
    "system_architect": {"list_files", "read_file", "search_text"},
    "code_architect": {"list_files", "read_file", "search_text"},
    "coder": {"list_files", "read_file", "search_text", "write_file", "delete_file", "run_check"},
    "tester": {"list_files", "read_file", "search_text", "write_file", "run_check"},
    "reviewer": {"list_files", "read_file", "search_text"},
    "debugger": {"list_files", "read_file", "search_text", "run_check"},
    "documentor": {"list_files", "read_file", "search_text", "write_file"},
}


def load_settings(path: Path) -> Settings:
    with path.open("rb") as handle:
        return Settings.model_validate(tomllib.load(handle))


def load_task(path: Path, settings: Settings) -> Task:
    task = Task.model_validate_json(path.read_text(encoding="utf-8-sig"))
    missing = {
        c for criterion in task.criteria for c in criterion.check_ids
    } - settings.checks.keys()
    if missing:
        raise ValueError(f"Task references unconfigured checks: {sorted(missing)}")
    return task


def load_roles(root: Path, settings: Settings) -> dict[str, Role]:
    raw = json.loads(files("goodhands").joinpath("defaults/roles.json").read_text("utf-8"))
    roles = {item["name"]: Role.model_validate(item) for item in raw}
    if settings.roles_dir:
        directory = (root / settings.roles_dir).resolve()
        for path in sorted(directory.glob("*.json")):
            role = Role.model_validate_json(path.read_text(encoding="utf-8"))
            roles[role.name] = role
    for name, role in roles.items():
        if name not in ROLES or not set(role.tools) <= PERMISSIONS[name]:
            raise ValueError(f"Role {name} requests unknown role or forbidden tools")
    return roles


def load_skills(root: Path, settings: Settings, names: list[str]) -> dict[str, str]:
    result = {}
    for name in dict.fromkeys(names):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError(f"Invalid skill name: {name}")
        local = (root / settings.skills_dir / f"{name}.md") if settings.skills_dir else None
        builtin = files("goodhands").joinpath(f"defaults/skills/{name}.md")
        if local and local.is_file():
            if local.stat().st_size > 32000:
                raise ValueError(f"Skill too large: {name}")
            result[name] = local.read_text(encoding="utf-8")
        elif builtin.is_file():
            result[name] = builtin.read_text("utf-8")
        else:
            raise ValueError(f"Unknown skill: {name}")
    return result
