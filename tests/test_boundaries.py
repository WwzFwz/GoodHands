import json
import os

import pytest

from goodhands.config import load_roles, load_skills
from goodhands.demo import DemoProvider
from goodhands.engine import Engine
from goodhands.models import Check
from goodhands.runner import Runner
from goodhands.tools import ToolExecutor
from goodhands.workspace import digest, inventory, safe_path


@pytest.mark.parametrize(
    "relative",
    [
        "../outside",
        "/absolute",
        "C:/secret",
        ".git/config",
        ".GIT/config",
        ".env",
        ".ENV.production",
        "foo/.aws/key",
        "file.txt:secret",
        "CON",
        "foo/../bar",
        "file.",
    ],
)
def test_paths_reject_escape_and_secrets(project, relative):
    with pytest.raises(ValueError):
        safe_path(project, relative)


def test_snapshot_omits_secrets(project):
    (project / ".env").write_text("SECRET=never-read")
    (project / "private.pem").write_text("secret")
    assert not {".env", "private.pem"} & inventory(project).keys()


def test_symlink_escape_is_rejected(project, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("outside")
    link = project / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is unavailable for this Windows account")
    with pytest.raises(ValueError):
        safe_path(project, "link.txt")
    assert "link.txt" not in inventory(project)


def test_permissions_hash_guard_and_test_protection(store, settings, task):
    engine = Engine(store, DemoProvider(), allow_local=True)
    state = engine.create(task, settings)
    tools = ToolExecutor(store, state, settings, task, True)
    with pytest.raises(ValueError, match="not permitted"):
        tools.execute(
            "reviewer",
            {"read_file"},
            "write_file",
            {"path": "calculator.py", "content": "bad", "expected_sha256": None},
        )
    with pytest.raises(ValueError, match="Stale"):
        tools.execute(
            "coder",
            {"write_file"},
            "write_file",
            {"path": "calculator.py", "content": "bad", "expected_sha256": "wrong"},
        )
    task.editable_paths = ["**"]
    with pytest.raises(ValueError, match="cannot modify"):
        tools.execute(
            "coder",
            {"write_file"},
            "write_file",
            {"path": "tests/test_calculator.py", "content": "", "expected_sha256": None},
        )
    with pytest.raises(ValueError, match="cannot modify"):
        tools.execute(
            "tester",
            {"write_file"},
            "write_file",
            {"path": "calculator.py", "content": "", "expected_sha256": None},
        )
    tools.execute(
        "tester",
        {"write_file"},
        "write_file",
        {"path": "tests/generated/test_edge.py", "content": "# new test", "expected_sha256": None},
    )
    assert (tools.workspace / "tests/generated/test_edge.py").exists()
    if os.name == "nt":
        with pytest.raises(ValueError, match="cannot modify"):
            tools.permitted_write("coder", "TESTS/test_calculator.py")


def test_changed_file_invalidates_evidence(store, settings, task):
    state = Engine(store, DemoProvider(), allow_local=True).create(task, settings)
    tools = ToolExecutor(store, state, settings, task, True)
    tools.execute("tester", {"run_check"}, "run_check", {"check_id": "unit"})
    assert state["evidence"]
    tools.execute(
        "coder",
        {"write_file"},
        "write_file",
        {
            "path": "calculator.py",
            "expected_sha256": digest(tools.workspace / "calculator.py"),
            "content": "def add(a,b): return a+b\n",
        },
    )
    assert state["evidence"] == {}


def test_runner_removes_provider_credentials(project, settings, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-never-in-child")
    result = Runner(settings.runner, True).run(
        project,
        Check(
            argv=[
                "python",
                "-c",
                "import os; assert 'OPENROUTER_API_KEY' not in os.environ; print('ok')",
            ]
        ),
    )
    assert result["exit_code"] == 0
    assert "test-secret" not in result["output"]


def test_runner_timeout_is_not_success(project, settings):
    result = Runner(settings.runner, True).run(
        project, Check(argv=["python", "-c", "import time; time.sleep(20)"], timeout_seconds=1)
    )
    assert result["timed_out"] and result["exit_code"] != 0


def test_check_mutating_workspace_is_not_evidence(store, settings, task):
    settings.checks["mutate"] = Check(
        argv=[
            "python",
            "-c",
            "from pathlib import Path; Path('calculator.py').write_text('# mutated')",
        ]
    )
    state = Engine(store, DemoProvider(), allow_local=True).create(task, settings)
    tools = ToolExecutor(store, state, settings, task, True)
    result = tools.execute("tester", {"run_check"}, "run_check", {"check_id": "mutate"})
    assert result["workspace_mutated"] and not result["passed"]


def test_role_plugin_cannot_elevate_permissions(project, settings):
    directory = project / "roles"
    directory.mkdir()
    (directory / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "instructions": "Review", "tools": ["write_file"]})
    )
    settings.roles_dir = "roles"
    with pytest.raises(ValueError, match="forbidden"):
        load_roles(project, settings)


def test_missing_skill_is_not_silently_ignored(project, settings):
    with pytest.raises(ValueError, match="Unknown skill"):
        load_skills(project, settings, ["does-not-exist"])
