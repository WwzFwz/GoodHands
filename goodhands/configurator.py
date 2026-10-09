"""Dedicated configuration agent: language -> isolated draft -> deterministic validation."""

import json
import time
import uuid
from importlib.resources import files
from pathlib import Path

from pydantic import Field

from .config import load_skills
from .inference import BudgetedRequests, Halt
from .models import ROLES, Contract, Settings
from .provider import ChatProvider, Provider, ProviderError
from .registry import builtin_roles, compile_registry, parse_role, read_role, roles_directory
from .store import Store
from .workspace import (
    apply_changes,
    changed_files,
    copy_snapshot,
    digest,
    fingerprint,
    safe_path,
)


class SkillDraft(Contract):
    path: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=32000)


class ConfigurationDraft(Contract):
    summary: str = Field(min_length=1, max_length=2000)
    questions: list[str] = Field(default_factory=list, max_length=10)
    readme: str = Field(default="", max_length=32000)
    skills: list[SkillDraft] = Field(default_factory=list, max_length=20)


def check_draft(root: Path, settings: Settings, target: str, current, draft: ConfigurationDraft):
    """Validate every model-controlled path and capability before writing anything."""
    if draft.questions:
        if draft.readme or draft.skills:
            raise ValueError("Clarification cannot be combined with a draft")
        return {}
    role = parse_role(draft.readme)
    if role.name != current.name or role.tools != current.tools or role.enabled != current.enabled:
        raise ValueError("Configurator cannot change role identity, tools, or enabled state")
    contents = {f"{target}/README.md": draft.readme}
    for skill in draft.skills:
        if not skill.path.startswith("skills/") or not skill.path.endswith(".md"):
            raise ValueError("Configurator skills must be under skills/ and end in .md")
        if skill.path not in role.skills:
            raise ValueError(f"Submitted skill is not explicitly included: {skill.path}")
        relative = f"{target}/{skill.path}"
        if relative.casefold() in {p.casefold() for p in contents}:
            raise ValueError("Duplicate draft path")
        contents[relative] = skill.content
    for relative, text in contents.items():
        safe_path(root, relative)
        if len(text.encode("utf-8")) > 32000:
            raise ValueError("Draft file exceeds 32000 bytes")
    return contents


def configure(
    store: Store,
    settings: Settings,
    role_name: str,
    request: str,
    provider: Provider | None = None,
    profile_name: str | None = None,
) -> dict:
    if role_name not in ROLES or not request.strip() or len(request) > 16000:
        raise ValueError("Choose an existing role and supply a request of 1..16000 characters")
    profile_name = profile_name or settings.role_profiles.get("configurator", "default")
    if profile_name not in settings.profiles:
        raise ValueError(f"Unknown profile: {profile_name}")
    provider = provider or ChatProvider(settings.provider)
    if isinstance(provider, ChatProvider):
        provider.preflight()
        if settings.profiles[profile_name].model == "SET_MODEL_ID":
            raise ValueError("Set a real model ID before using Configurator")
    directory = roles_directory(store.project, settings)
    # Honour either supported folder spelling, and do not create competing sources.
    candidates = [directory / role_name, directory / role_name.replace("_", "-")]
    folder = next((p for p in candidates if p.is_dir()), candidates[0])
    target = folder.relative_to(store.project).as_posix()
    path = safe_path(store.project, f"{target}/README.md")
    current = builtin_roles()[role_name]
    source, errors, skill_texts = "", [], {}
    if path.is_file():
        if path.stat().st_size > 32000:
            raise ValueError("Existing README too large")
        source = path.read_text("utf-8-sig")
        try:
            current = parse_role(source)
            if current.name != role_name:
                raise ValueError("Existing README has a different role name")
            resolved = read_role(store.project, path)
            skill_texts = load_skills(store.project, settings, resolved.skills)
        except ValueError as error:
            errors.append(str(error))
            # Preserve parsable capability restrictions even when skill references are broken.
            if current.name != role_name:
                current = builtin_roles()[role_name]
    if not source and any(directory.glob("*.json")):
        raise ValueError("Migrate legacy role JSON to Markdown before using Configurator")
    run_id = uuid.uuid4().hex[:16]
    home = store.home(run_id)
    home.mkdir(parents=True)
    copy_snapshot(store.project, home / "base")
    copy_snapshot(home / "base", home / "workspace")
    state = {
        "id": run_id,
        "kind": "configuration",
        "created_at": time.time(),
        "status": "configuring",
        "message": "",
        "task": {"title": f"Configure {role_name}"},
        "settings": settings.model_dump(),
        "settings_source_hash": (
            digest(store.project / "goodhands.toml")
            if (store.project / "goodhands.toml").is_file()
            else None
        ),
        "target": target,
        "current_role": current.model_dump(),
        "request": request,
        "model_calls": 0,
        "charged_usd": 0.0,
        "cost_accounting": {},
        "elapsed_seconds": 0.0,
        "session_started": time.time(),
        "pending_request": None,
        "simulated": provider.simulated,
    }
    store.save(state)
    instructions = files("goodhands").joinpath("defaults/configurator.md").read_text("utf-8")
    state["instructions"] = instructions
    messages = [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "request": request,
                    "role": current.model_dump(),
                    "existing_readme": source,
                    "included_skills": skill_texts,
                    "source_errors": errors,
                },
                ensure_ascii=False,
            ),
        },
    ]
    schema = [
        {
            "type": "function",
            "function": {
                "name": "submit_configuration",
                "description": "Submit a draft or clarification questions",
                "parameters": ConfigurationDraft.model_json_schema(),
            },
        }
    ]
    requester = BudgetedRequests(store, state, settings, provider)
    try:
        for _ in range(min(3, settings.policy.max_role_turns)):
            message = requester.request(
                "configurator", settings.profiles[profile_name], messages, schema
            )
            calls = message.get("tool_calls") or []
            try:
                if len(calls) != 1 or not isinstance(calls[0], dict):
                    raise ValueError("Call submit_configuration exactly once")
                function = calls[0].get("function") or {}
                if function.get("name") != "submit_configuration":
                    raise ValueError("Only submit_configuration is available")
                draft = ConfigurationDraft.model_validate_json(function.get("arguments", ""))
                contents = check_draft(home / "workspace", settings, target, current, draft)
                if draft.questions:
                    state.update(
                        status="config_needs_input",
                        questions=draft.questions,
                        message=draft.summary,
                    )
                    break
                # Reset only the prior attempt's generated files before validating the new draft.
                for relative in state.get("draft_files", []):
                    dest = safe_path(home / "workspace", relative)
                    baseline = safe_path(home / "base", relative)
                    if baseline.is_file():
                        dest.write_bytes(baseline.read_bytes())
                    elif dest.exists():
                        dest.unlink()
                state["draft_files"] = list(contents)
                for relative, text in contents.items():
                    dest = safe_path(home / "workspace", relative)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(text, encoding="utf-8")
                manifest = compile_registry(home / "workspace", settings)
                artifact = store.artifact(
                    run_id,
                    "configuration",
                    {
                        "draft": draft.model_dump(),
                        "compiled": manifest,
                    },
                )
                state.update(
                    status="config_ready",
                    message=draft.summary,
                    artifact=artifact,
                    snapshot=fingerprint(home / "workspace"),
                )
                break
            except (ValueError, TypeError, AttributeError) as error:
                state["message"] = str(error)[:2000]
                store.event(run_id, "configuration_invalid", error=state["message"])
                # No tools executed by the model; summarize rejected output as data for retry.
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "rejected_response": message,
                                "validation_error": state["message"],
                                "instruction": "Return a corrected complete draft using submit_configuration.",
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
        else:
            state["status"] = "config_blocked"
    except (Halt, ProviderError, ValueError, OSError) as error:
        state.update(status="config_blocked", message=str(error))
    finally:
        state["elapsed_seconds"] = time.time() - state["session_started"]
        store.save(state)
    return state


def apply_configuration(store: Store, run_id: str) -> list[str]:
    state = store.get(run_id)
    if state.get("kind") != "configuration" or state["status"] != "config_ready":
        raise ValueError("Only a validated configuration draft can be applied")
    home = store.home(run_id)
    if fingerprint(home / "workspace") != state["snapshot"]:
        raise ValueError("Configuration draft changed after validation")
    settings = Settings.model_validate(state["settings"])
    config_path = store.project / "goodhands.toml"
    if (digest(config_path) if config_path.is_file() else None) != state["settings_source_hash"]:
        raise ValueError("Project settings changed after configuration draft was created")
    compile_registry(home / "workspace", settings)
    for relative in changed_files(home / "base", home / "workspace"):
        if relative not in state["draft_files"]:
            raise ValueError("Configuration draft contains an unauthorized file")
    changes = apply_changes(store.project, home / "base", home / "workspace", home / "apply-backup")
    state["status"] = "config_applied"
    store.save(state)
    return changes
