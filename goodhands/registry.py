"""Markdown role sources, explicit skill references, and deterministic compilation."""

import json
import posixpath
from importlib.resources import files
from pathlib import Path

import yaml

from .config import PERMISSIONS, Role, load_roles, load_skills
from .models import ROLES, Settings
from .store import atomic_json
from .workspace import safe_path


class UniqueLoader(yaml.SafeLoader):
    """Reject ambiguous keys and aliases instead of silently overriding settings."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are not supported in role metadata")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Role metadata needs unique string keys")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_role(text: str) -> Role:
    if len(text.encode("utf-8")) > 32000:
        raise ValueError("Role README exceeds 32000 bytes")
    lines = text.removeprefix("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(
            "README needs YAML metadata between --- lines; use agents configure for help"
        )
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Role metadata is missing closing ---")
    try:
        metadata = yaml.load("".join(lines[1:end]), Loader=UniqueLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid role YAML: {error}") from None
    if not isinstance(metadata, dict) or "instructions" in metadata:
        raise ValueError("Metadata must be a mapping; instructions belong in the Markdown body")
    body = "".join(lines[end + 1 :])
    if not body.strip():
        raise ValueError("Role instructions cannot be empty")
    role = Role.model_validate({**metadata, "instructions": body})
    if role.name not in ROLES or not set(role.tools) <= PERMISSIONS[role.name]:
        raise ValueError(f"Role {role.name} requests unknown role or forbidden tools")
    if len(role.skills) != len(set(role.skills)) or len(role.tools) != len(set(role.tools)):
        raise ValueError("Duplicate skills or tools")
    return role


def builtin_roles() -> dict[str, Role]:
    directory = files("goodhands").joinpath("defaults/agents")
    return {
        name: parse_role(directory.joinpath(f"{name}/README.md").read_text("utf-8"))
        for name in ROLES
    }


def roles_directory(root: Path, settings: Settings) -> Path:
    directory = safe_path(root, settings.roles_dir or "agents")
    if settings.roles_dir and not directory.is_dir():
        raise ValueError(f"Configured roles_dir does not exist: {settings.roles_dir}")
    return directory


def read_role(root: Path, path: Path) -> Role:
    if path.stat().st_size > 32000:
        raise ValueError(f"Role README too large: {path.name}")
    role = parse_role(path.read_text(encoding="utf-8-sig"))
    if path.parent.name.replace("-", "_") != role.name:
        raise ValueError("Role folder must match metadata name (hyphens or underscores)")
    resolved = []
    for skill in role.skills:
        if skill.endswith(".md"):
            # Permit ../ shared references only after normalizing within the project.
            if skill.startswith(("/", "\\")) or ":" in skill or "\\" in skill:
                raise ValueError("Skill references must be relative POSIX paths")
            relative = posixpath.normpath(f"{path.parent.relative_to(root).as_posix()}/{skill}")
            safe_path(root, relative)
            resolved.append(relative)
        else:
            resolved.append(skill)
    role.skills = resolved
    return role


def compile_registry(root: Path, settings: Settings) -> dict:
    roles = load_roles(root, settings)
    return {
        "schema_version": 1,
        "roles": {name: role.model_dump() for name, role in sorted(roles.items())},
        "skills": {
            name: load_skills(root, settings, role.skills) for name, role in sorted(roles.items())
        },
    }


def write_compiled(root: Path, settings: Settings, state_root: Path) -> Path:
    manifest = compile_registry(root, settings)
    # safe_path on the state root prevents following user-created compiled symlinks.
    destination = safe_path(state_root, "compiled/roles.json")
    safe_path(state_root, "compiled/roles.tmp")
    atomic_json(destination, manifest)
    return destination


def scaffold(root: Path, settings: Settings) -> list[str]:
    directory = safe_path(root, settings.roles_dir or "agents")
    source = files("goodhands").joinpath("defaults/agents")
    paths = [directory / name / "README.md" for name in ROLES]
    if any(path.exists() for path in paths) or directory.exists():
        raise ValueError("Refusing to overwrite an existing agents directory")
    for name, path in zip(ROLES, paths):
        safe_path(root, path.relative_to(root).as_posix())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.joinpath(f"{name}/README.md").read_text("utf-8"), encoding="utf-8")
    return [path.relative_to(root).as_posix() for path in paths]


def render_role(role: Role) -> str:
    metadata = role.model_dump(exclude={"instructions"})
    # JSON values are valid YAML, and keep version strings unambiguous.
    return (
        "---\n"
        + "\n".join(f"{key}: {json.dumps(value)}" for key, value in metadata.items())
        + "\n---\n"
        + role.instructions
    )
