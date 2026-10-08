import json

import pytest

from goodhands.demo import CALCULATOR, CORRECT_CALCULATOR, DemoProvider
from goodhands.engine import Engine
from goodhands.models import Profile
from goodhands.provider import ProviderError
from goodhands.workspace import apply_changes, diff_text, fingerprint


def test_full_workflow_real_checks_and_conflict_safe_apply(store, settings, task, project):
    engine = Engine(store, DemoProvider(), allow_local=True)
    state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "completed_simulated"
    assert state["charged_usd"] == 0
    assert (project / "calculator.py").read_text() == CALCULATOR
    home = store.home(state["id"])
    assert "return a + b" in diff_text(home / "base", home / "workspace")
    report = store.read_artifact(state["id"], state["evidence"]["unit"])
    assert report["passed"] and report["snapshot"] == state["snapshot"]
    assert "Ran 3 tests" in report["output"]
    (project / "calculator.py").write_text("# concurrent human edit\n")
    with pytest.raises(ValueError, match="Source conflict"):
        apply_changes(project, home / "base", home / "workspace", home / "apply-backup")
    assert (project / "calculator.py").read_text() == "# concurrent human edit\n"
    (project / "calculator.py").write_text(CALCULATOR)
    changes = apply_changes(project, home / "base", home / "workspace", home / "apply-backup")
    assert changes == ["calculator.py"]
    assert (project / "calculator.py").read_text() == CORRECT_CALCULATOR


def test_debugger_only_after_bounded_repairs(store, settings, task):
    provider = DemoProvider(fail_coder_attempts=3)
    settings.policy.escalation_profile = "strong"
    settings.profiles["strong"] = Profile(model="simulated-strong")
    engine = Engine(store, provider, allow_local=True)
    state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "completed_simulated"
    assert state["repairs"] == 2 and state["debugger_used"] and state["escalated"]
    stages = [entry["stage"] for entry in state["history"]]
    assert stages.count("debugger") == 1 and stages.count("coder") == 4


def test_permanent_failure_never_completes(store, settings, task):
    engine = Engine(store, DemoProvider(fail_coder_attempts=100), allow_local=True)
    state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "blocked"
    assert "limit reached" in state["message"]
    assert state["debugger_used"]


def test_quick_low_risk_skips_optional_roles(store, settings, task):
    task.risk = "low"
    engine = Engine(store, DemoProvider(), allow_local=True)
    state = engine.execute(engine.create(task, settings, preset="quick")["id"])
    assert state["status"] == "completed_simulated"
    assert [h["stage"] for h in state["history"]] == ["consultant", "coder"]


def test_documentation_triggers_fresh_review_and_final_checks(store, settings, task):
    task.document = True
    engine = Engine(store, DemoProvider(), allow_local=True)
    state = engine.execute(engine.create(task, settings, preset="system")["id"])
    assert state["status"] == "completed_simulated"
    assert (store.home(state["id"]) / "workspace/docs/calculator.md").is_file()
    assert "system_architect" in state["results"]
    reviewer = store.read_artifact(state["id"], state["results"]["reviewer"])
    assert reviewer["snapshot"] == state["snapshot"]
    assert sum(h["stage"] == "reviewer" for h in state["history"]) == 2


@pytest.mark.parametrize(
    "role",
    [
        "consultant",
        "system_architect",
        "code_architect",
        "coder",
        "reviewer",
        "debugger",
        "documentor",
    ],
)
def test_standalone_role_is_not_full_acceptance(store, settings, task, role):
    engine = Engine(store, DemoProvider(), allow_local=True)
    state = engine.execute(engine.create(task, settings, mode=role)["id"])
    assert state["status"] == "report_ready"
    assert [h["stage"] for h in state["history"]] == [role]


def test_resume_preserves_completed_stages_and_frozen_config(store, settings, task):
    class InterruptOnce(DemoProvider):
        interrupted = False

        def complete(self, **kwargs):
            if kwargs["stage"] == "coder" and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return super().complete(**kwargs)

    engine = Engine(store, InterruptOnce(), allow_local=True)
    state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "interrupted"
    old_calls = state["model_calls"]
    state = engine.execute(state["id"])
    assert state["status"] == "completed_simulated"
    assert state["model_calls"] > old_calls
    # One initial consultation plus one refresh after Coder, not a replay of planning.
    assert sum(h["stage"] == "consultant" for h in state["history"]) == 2
    assert sum(h["stage"] == "code_architect" for h in state["history"]) == 1


def test_resume_requires_reconciliation_for_external_changes(store, settings, task):
    engine = Engine(store, DemoProvider(), allow_local=True)
    state = engine.create(task, settings)
    workspace = store.home(state["id"]) / "workspace"
    (workspace / "calculator.py").write_text(CORRECT_CALCULATOR)
    with pytest.raises(ValueError, match="reconcile"):
        engine.execute(state["id"])
    state = engine.execute(state["id"], reconcile=True)
    assert state["status"] == "completed_simulated"
    assert state["snapshot"] == fingerprint(workspace)


def test_missing_runner_authorization_fails_before_model_calls(store, settings, task):
    engine = Engine(store, DemoProvider())
    with pytest.raises(ValueError, match="allow-local-exec"):
        engine.create(task, settings)
    assert store.list() == []


def test_unknown_usage_is_reserved_and_cannot_escape_budget(store, settings, task):
    class UnknownUsage(DemoProvider):
        simulated = False

        def complete(self, **kwargs):
            data = super().complete(**kwargs)
            data.pop("usage")
            return data

    settings.profiles["default"].model = "test-model"
    settings.policy.budget_usd = 0.15
    engine = Engine(store, UnknownUsage(), allow_local=True)
    state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "blocked"
    assert state["model_calls"] == 1
    assert state["charged_usd"] == pytest.approx(0.10)
    assert state["cost_accounting"]["unknown_reserved"] == 1


def test_network_failure_retains_reservation_and_no_auto_retry(store, settings, task):
    class BrokenProvider:
        simulated = False

        def complete(self, **kwargs):
            raise ProviderError("Network failure")

    settings.profiles["default"].model = "test-model"
    engine = Engine(store, BrokenProvider(), allow_local=True)
    state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "blocked" and state["model_calls"] == 1
    assert state["charged_usd"] == pytest.approx(0.1)
    assert state["cost_accounting"]["unknown_reserved"] == 1


def test_mixed_submit_and_write_batch_performs_no_write(store, settings, task):
    class MixedBatch(DemoProvider):
        def complete(self, **kwargs):
            data = super().complete(**kwargs)
            if kwargs["stage"] == "coder":
                data["choices"][0]["message"]["tool_calls"] = [
                    {
                        "id": "w",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "new.py", "content": "bad", "expected_sha256": None}
                            ),
                        },
                    },
                    {"id": "s", "function": {"name": "submit_result", "arguments": "{}"}},
                ]
            return data

    task.editable_paths.append("new.py")
    engine = Engine(store, MixedBatch(), allow_local=True)
    state = engine.execute(engine.create(task, settings)["id"])
    assert state["status"] == "blocked"
    assert not (store.home(state["id"]) / "workspace/new.py").exists()
