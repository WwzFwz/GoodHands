"""Load declarative roles and skills without executing plugin code."""

import re
import tomllib
from importlib.resources import files
from pathlib import Path

from pydantic import Field

from .models import ROLES, Contract, Settings, Task
from .workspace import safe_path


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
    from .registry import builtin_roles, read_role, roles_directory

    roles = builtin_roles()
    directory = roles_directory(root, settings)
    seen = set()
    for path in sorted([*directory.glob("*.json"), *directory.glob("*/README.md")]):
        path = safe_path(root, path.relative_to(root).as_posix())
        if path.suffix == ".json":
            role = Role.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            role = read_role(root, path)
        if role.name in seen:
            raise ValueError(f"Duplicate role override: {role.name}")
        seen.add(role.name)
        roles[role.name] = role
    for name, role in roles.items():
        if name not in ROLES or not set(role.tools) <= PERMISSIONS[name]:
            raise ValueError(f"Role {name} requests unknown role or forbidden tools")
    return roles


def load_skills(root: Path, settings: Settings, names: list[str]) -> dict[str, str]:
    result = {}
    for name in dict.fromkeys(names):
        if name.endswith(".md"):
            local = safe_path(root, name)
            if not local.is_file() or local.stat().st_size > 32000:
                raise ValueError(f"Missing or oversized skill: {name}")
            result[name] = local.read_text(encoding="utf-8-sig")
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError(f"Invalid skill name: {name}")
        local = safe_path(root, f"{settings.skills_dir}/{name}.md") if settings.skills_dir else None
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
