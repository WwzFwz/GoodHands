import json

import pytest

from goodhands.cli import main
from goodhands.config import load_roles, load_skills
from goodhands.configurator import apply_configuration, configure
from goodhands.demo import ConfigurationDemoProvider, DemoProvider
from goodhands.engine import Engine
from goodhands.models import Profile, Settings
from goodhands.provider import ProviderError
from goodhands.registry import builtin_roles, compile_registry, parse_role, render_role, scaffold
from goodhands.store import Store


def write_role(project, role, folder=None):
    path = project / "agents" / (folder or role.name) / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_role(role), encoding="utf-8")
    return path


def test_markdown_source_preserves_body_and_resolves_only_explicit_skills(project, settings):
    role = builtin_roles()["coder"]
    role.instructions = "# Coder\n\nKeep **this** verbatim.\n"
    role.skills = ["skills/nested/implementation.md", "../../skills/shared/python.md", "python"]
    write_role(project, role)
    for path, content in {
        "agents/coder/skills/nested/implementation.md": "# Implementation",
        "skills/shared/python.md": "# Shared Python",
        "agents/coder/skills/unused.md": "must not enter context",
    }.items():
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    manifest = compile_registry(project, settings)
    assert manifest["roles"]["coder"]["instructions"] == role.instructions
    assert set(manifest["skills"]["coder"]) == {
        "agents/coder/skills/nested/implementation.md",
        "skills/shared/python.md",
        "python",
    }
    assert "must not enter context" not in json.dumps(manifest)
    assert compile_registry(project, settings) == manifest


@pytest.mark.parametrize(
    "reference",
    [
        "../../../outside.md",
        "../../.env/secret.md",
        "/absolute.md",
        "C:/secret.md",
        "skills/../x/../../../../escape.md",
    ],
)
def test_skill_references_cannot_escape_project(project, settings, reference):
    role = builtin_roles()["coder"]
    role.skills = [reference]
    write_role(project, role)
    with pytest.raises(ValueError):
        compile_registry(project, settings)


@pytest.mark.parametrize(
    "text",
    [
        "# No metadata",
        "---\nname: coder\n",
        "---\n[]\n---\nbody",
        "---\nname: coder\nname: tester\n---\nbody",
        "---\nname: &role coder\nversion: *role\n---\nbody",
        "---\nname: !!python/object:thing {}\n---\nbody",
        "---\nname: coder\ntools: []\n---\n \n",
    ],
)
def test_invalid_or_ambiguous_yaml_is_rejected(text):
    with pytest.raises(ValueError):
        parse_role(text)


def test_legacy_override_works_but_duplicate_markdown_fails(project, settings):
    role = builtin_roles()["coder"]
    role.instructions = "Legacy override"
    directory = project / "agents"
    directory.mkdir()
    (directory / "coder.json").write_text(role.model_dump_json())
    assert load_roles(project, settings)["coder"].instructions == "Legacy override"
    write_role(project, role)
    with pytest.raises(ValueError, match="Duplicate"):
        load_roles(project, settings)


def test_unknown_skill_and_forbidden_role_permissions_fail(project, settings):
    role = builtin_roles()["reviewer"]
    role.tools.append("write_file")
    path = write_role(project, role)
    with pytest.raises(ValueError, match="forbidden"):
        load_roles(project, settings)
    role.tools.remove("write_file")
    role.skills = ["skills/missing.md"]
    path.write_text(render_role(role))
    with pytest.raises(ValueError, match="Missing"):
        compile_registry(project, settings)


def test_markdown_loaded_by_engine_and_frozen_in_checkpoint(store, settings, task):
    role = builtin_roles()["coder"]
    role.skills = ["skills/implementation.md"]
    path = write_role(store.project, role)
    skill = path.parent / "skills/implementation.md"
    skill.parent.mkdir()
    skill.write_text("Original skill")
    engine = Engine(store, DemoProvider(), allow_local=True)
    state = engine.create(task, settings)
    skill.write_text("Changed after checkpoint")
    assert state["skills"]["coder"]["agents/coder/skills/implementation.md"] == "Original skill"
    assert engine.execute(state["id"])["status"] == "completed_simulated"


def test_configuration_draft_applies_then_engine_uses_it(store, settings, task):
    state = configure(
        store, settings, "coder", "Add implementation skill", ConfigurationDemoProvider()
    )
    assert state["status"] == "config_ready"
    assert not (store.project / "agents").exists()
    changes = apply_configuration(store, state["id"])
    assert set(changes) == {"agents/coder/README.md", "agents/coder/skills/implementation.md"}
    role = load_roles(store.project, settings)["coder"]
    assert "Ikuti kontrak" in role.instructions
    assert "kontrak" in next(iter(load_skills(store.project, settings, role.skills).values()))
    engine = Engine(store, DemoProvider(), allow_local=True)
    run = engine.create(task, settings)
    assert engine.execute(run["id"])["status"] == "completed_simulated"
    with pytest.raises(ValueError, match="validated"):
        apply_configuration(store, state["id"])


class Responses(ConfigurationDemoProvider):
    def __init__(self, mutations):
        self.mutations = iter(mutations)

    def complete(self, **kwargs):
        response = super().complete(**kwargs)
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        draft = json.loads(function["arguments"])
        next(self.mutations)(draft)
        function["arguments"] = json.dumps(draft)
        return response


def test_configurator_repairs_invalid_format_with_bounded_feedback(store, settings):
    def break_format(draft):
        draft["readme"] = "Broken YAML"

    state = configure(
        store, settings, "coder", "Fix format", Responses([break_format, lambda d: None])
    )
    assert state["status"] == "config_ready"
    assert state["model_calls"] == 2
    assert "configuration_invalid" in (store.home(state["id"]) / "events.jsonl").read_text()


def test_configurator_does_not_apply_ambiguous_intent(store, settings):
    def ask(draft):
        draft.update(readme="", skills=[], questions=["Which coding conventions?"])

    state = configure(store, settings, "coder", "Make it right", Responses([ask]))
    assert state["status"] == "config_needs_input"
    assert state["questions"] == ["Which coding conventions?"]
    with pytest.raises(ValueError):
        apply_configuration(store, state["id"])
    assert not (store.project / "agents").exists()


@pytest.mark.parametrize("mutation", ["tools", "escape", "identity"])
def test_configurator_rejects_capability_changes_and_path_escape(store, settings, mutation):
    def bad(draft):
        if mutation == "escape":
            draft["skills"][0]["path"] = "skills/../../../../stolen.md"
            draft["readme"] = draft["readme"].replace(
                "skills/implementation.md", draft["skills"][0]["path"]
            )
        else:
            role = parse_role(draft["readme"])
            if mutation == "tools":
                role.tools = []
            else:
                role.name = "tester"
            draft["readme"] = render_role(role)

    state = configure(store, settings, "coder", "Change configuration", Responses([bad] * 3))
    assert state["status"] == "config_blocked"
    assert state["model_calls"] == 3
    assert not (store.project / "agents").exists()


@pytest.mark.parametrize("location", ["source", "draft"])
def test_configuration_apply_rejects_changes_after_validation(store, settings, location):
    state = configure(store, settings, "coder", "Draft", ConfigurationDemoProvider())
    root = store.project if location == "source" else store.home(state["id"]) / "workspace"
    (root / "calculator.py").write_text("External change")
    with pytest.raises(ValueError):
        apply_configuration(store, state["id"])
    assert not (store.project / "agents").exists()


def test_failed_live_request_retains_reservation_without_replay(store, settings):
    class Failure:
        simulated = False
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            raise ProviderError("Connection failed; charge unknown")

    provider = Failure()
    settings.profiles["default"] = Profile(model="test", request_reserve_usd=0.2)
    state = configure(store, settings, "coder", "Draft", provider)
    assert provider.calls == 1
    assert state["status"] == "config_blocked"
    assert state["charged_usd"] == 0.2
    assert state["pending_request"] is not None


def test_configurator_budget_blocks_call_before_inference(store, settings):
    class Never:
        simulated = False

        def complete(self, **kwargs):
            pytest.fail("No budget for inference")

    settings.policy.budget_usd = 0.01
    state = configure(store, settings, "coder", "Draft", Never())
    assert state["model_calls"] == 0
    assert state["status"] == "config_blocked"


def test_broken_readme_can_be_repaired_and_existing_instructions_are_supplied(store, settings):
    path = store.project / "agents/coder/README.md"
    path.parent.mkdir(parents=True)
    path.write_text("Coder: follow the plan; metadata is missing.")

    class Repair(ConfigurationDemoProvider):
        def complete(self, **kwargs):
            context = json.loads(kwargs["messages"][1]["content"])
            assert "metadata is missing" in context["existing_readme"]
            assert context["source_errors"]
            return super().complete(**kwargs)

    state = configure(store, settings, "coder", "Repair format", Repair())
    assert state["status"] == "config_ready"
    apply_configuration(store, state["id"])
    compile_registry(store.project, settings)


def test_cli_scaffold_validate_compile_and_source_freshness(project, capsys):
    args = ["--project", str(project)]
    assert main(["agents", "init", *args]) == 0
    assert main(["agents", "init", *args]) == 2
    assert main(["agents", "validate", *args]) == 0
    assert main(["agents", "compile", *args]) == 0
    compiled = project / ".goodhands/compiled/roles.json"
    before = compiled.read_bytes()
    assert main(["agents", "compile", *args]) == 0
    assert compiled.read_bytes() == before
    path = project / "agents/coder/README.md"
    path.write_text(path.read_text() + "\nNew source instruction.\n")
    assert "New source instruction" in load_roles(project, Settings())["coder"].instructions
    assert main(["agents", "compile", *args]) == 0
    assert compiled.read_bytes() != before


def test_profile_mapping_supports_configurator_without_adding_engineering_stage():
    Settings(role_profiles={"configurator": "default"})
    assert "configurator" not in builtin_roles()


def test_configuration_apply_rejects_changed_project_settings(store, settings):
    state = configure(store, settings, "coder", "Draft", ConfigurationDemoProvider())
    (store.project / "goodhands.toml").write_text('roles_dir = "different"\n')
    with pytest.raises(ValueError, match="settings changed"):
        apply_configuration(store, state["id"])


def test_retry_removes_files_from_rejected_draft(store, settings):
    def missing_reference(draft):
        role = parse_role(draft["readme"])
        role.skills.append("skills/missing.md")
        draft["readme"] = render_role(role)

    def different_skill(draft):
        draft["readme"] = draft["readme"].replace("implementation.md", "corrected.md")
        draft["skills"][0]["path"] = "skills/corrected.md"

    state = configure(
        store, settings, "coder", "Draft", Responses([missing_reference, different_skill])
    )
    assert state["status"] == "config_ready"
    apply_configuration(store, state["id"])
    assert (store.project / "agents/coder/skills/corrected.md").is_file()
    assert not (store.project / "agents/coder/skills/implementation.md").exists()


def test_cli_demo_diff_apply_and_no_live_resume(project, capsys):
    assert main(["agents", "demo", "--project", str(project)]) == 0
    demo = next((project / ".goodhands/config-demos").iterdir())
    store = Store(demo)
    run_id = store.list()[0]["id"]
    store.close()
    args = [run_id, "--project", str(demo)]
    assert main(["agents", "diff", *args]) == 0
    assert main(["resume", *args]) == 2
    assert main(["agents", "apply", *args]) == 0
    assert main(["agents", "validate", "--project", str(demo)]) == 0


def test_configured_role_directory_is_used(project):
    settings = Settings(roles_dir="custom-agents")
    scaffold(project, settings)
    assert len(load_roles(project, settings)) == 8
