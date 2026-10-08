import json

import pytest

from goodhands.cli import main
from goodhands.config import load_settings
from goodhands.store import Store, project_lock


def test_init_is_non_destructive_and_vercel_is_optional(project, capsys):
    assert (
        main(
            [
                "init",
                "--project",
                str(project),
                "--provider",
                "vercel",
                "--model",
                "chosen/model",
                "--runner",
                "local",
            ]
        )
        == 0
    )
    settings = load_settings(project / "goodhands.toml")
    assert settings.provider.kind == "compatible"
    assert settings.profiles["default"].model == "chosen/model"
    original = (project / "goodhands.toml").read_text()
    assert main(["init", "--project", str(project)]) == 2
    assert (project / "goodhands.toml").read_text() == original
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_doctor_no_key_does_not_call_model(project, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    main(["init", "--project", str(project), "--runner", "local"])
    assert main(["doctor", "--project", str(project)]) == 1
    assert "not set" in capsys.readouterr().out
    assert not (project / ".goodhands/state.db").exists()


def test_demo_cli_diff_apply_and_reject_stale_snapshot(tmp_path, capsys):
    project = tmp_path / "demo"
    assert main(["demo", "--directory", str(project)]) == 0
    store = Store(project)
    try:
        state = store.list()[0]
        run_id = state["id"]
        assert main(["show", run_id, "--project", str(project)]) == 0
        assert main(["diff", run_id, "--project", str(project)]) == 0
        workspace_file = store.home(run_id) / "workspace/calculator.py"
        verified = workspace_file.read_bytes()
        workspace_file.write_text("# altered after completion\n")
        assert main(["apply", run_id, "--project", str(project)]) == 2
        assert "return a - b" in (project / "calculator.py").read_text()
        workspace_file.write_bytes(verified)
        assert main(["apply", run_id, "--project", str(project)]) == 0
        assert "return a + b" in (project / "calculator.py").read_text()
        assert main(["apply", run_id, "--project", str(project)]) == 2
    finally:
        store.close()


def test_project_lock_rejects_overlapping_execution(tmp_path):
    with project_lock(tmp_path):
        with pytest.raises(ValueError, match="Another GoodHands"):
            with project_lock(tmp_path):
                pass
    with project_lock(tmp_path):
        pass


def test_invalid_task_references_fail_before_inference(project, monkeypatch, capsys):
    main(["init", "--project", str(project), "--runner", "local"])
    task_path = project / "task.json"
    data = json.loads(task_path.read_text())
    data["criteria"][0]["check_ids"] = ["missing"]
    task_path.write_text(json.dumps(data))
    assert main(["run", "--project", str(project), "--allow-local-exec"]) == 2
    assert "unconfigured checks" in capsys.readouterr().err
