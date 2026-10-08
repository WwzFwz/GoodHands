"""Command-line interface; secrets are supplied only through the environment."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import __version__
from .config import load_settings, load_task
from .demo import DemoProvider, create_demo
from .engine import Engine
from .models import ROLES, Check, Criterion, Policy, RunnerSettings, Settings, Task
from .store import Store, project_lock
from .workspace import apply_changes, diff_text, fingerprint


def config_template(provider: str, model: str, runner: str) -> str:
    base_url = (
        "https://openrouter.ai/api/v1"
        if provider == "openrouter"
        else "https://ai-gateway.vercel.sh/v1"
    )
    key_env = "OPENROUTER_API_KEY" if provider == "openrouter" else "AI_GATEWAY_API_KEY"
    # JSON string quoting is also valid for these TOML strings.
    return f'''# GoodHands configuration. Secrets belong in environment variables.
[provider]
kind = "{"openrouter" if provider == "openrouter" else "compatible"}"
base_url = "{base_url}"
api_key_env = "{key_env}"
timeout_seconds = 90

[profiles.default]
model = {json.dumps(model)}
max_output_tokens = 4096
# Conservative reservation per request, not a guaranteed provider spending cap.
request_reserve_usd = 0.10
# For APIs without usage.cost, set current prices to enable estimated accounting:
# input_usd_per_million = 0.0
# output_usd_per_million = 0.0

[policy]
budget_usd = 2.0
max_model_calls = 50
max_role_turns = 12
max_repairs = 2
max_run_seconds = 1800
max_context_chars = 60000
reviewer = "risk"
# escalation_profile = "strong"

[runner]
kind = "{runner}"
image = "python:3.12-slim"
memory = "512m"
cpus = 1.0

# Replace these checks with the actual project commands before a live run.
[checks.unit]
argv = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout_seconds = 60

# [profiles.strong]
# model = "SET_STRONG_MODEL_ID"
# request_reserve_usd = 0.30
# [role_profiles]
# system_architect = "strong"
# reviewer = "strong"
'''


def sample_task() -> Task:
    return Task(
        title="Fix addition",
        goal="Make add(a, b) return the sum while preserving its public API.",
        criteria=[
            Criterion(
                id="AC1",
                description="Positive, negative and zero inputs produce correct sums",
                check_ids=["unit"],
            )
        ],
        editable_paths=["calculator.py"],
        complexity="complex",
        risk="medium",
        skills=["python"],
    )


def print_state(state: dict, store: Store):
    print(
        json.dumps(
            {
                "run_id": state["id"],
                "status": state["status"],
                "message": state["message"],
                "simulated": state["simulated"],
                "charged_or_reserved_usd": round(state["charged_usd"], 6),
                "model_calls": state["model_calls"],
                "artifacts": str(store.home(state["id"]) / "artifacts"),
                "workspace": str(store.home(state["id"]) / "workspace"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="goodhands",
        description="Local engineering harness with explicit roles and verification evidence",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="Create config and sample task without overwriting files"
    )
    init.add_argument("--project", default=".")
    init.add_argument("--provider", choices=["openrouter", "vercel"], default="openrouter")
    init.add_argument("--model", default="SET_MODEL_ID")
    init.add_argument("--runner", choices=["docker", "local"], default="docker")
    demo = commands.add_parser(
        "demo", help="Run a deterministic offline example; no API account needed"
    )
    demo.add_argument(
        "--directory",
        help="New empty directory; default creates a unique folder in .goodhands/demos",
    )
    demo.add_argument(
        "--exercise-recovery",
        action="store_true",
        help="Simulate three unsuccessful patches before invoking Debugger",
    )
    doctor = commands.add_parser(
        "doctor", help="Inspect configuration and prerequisites without calling an LLM"
    )
    doctor.add_argument("--project", default=".")
    for command in ["run", "resume", "show", "runs", "diff", "apply"]:
        sub = commands.add_parser(command)
        sub.add_argument("--project", default=".")
        if command in {"resume", "show", "diff", "apply"}:
            sub.add_argument("run_id")
        if command in {"run", "resume"}:
            sub.add_argument(
                "--allow-local-exec",
                action="store_true",
                help="Authorize configured commands on the host for a trusted project; not a sandbox",
            )
        if command == "run":
            sub.add_argument("--task", default="task.json")
            sub.add_argument("--preset", choices=["quick", "feature", "system"], default="feature")
            sub.add_argument(
                "--role",
                choices=ROLES,
                help="Run only this role; output is not a verified full workflow",
            )
        if command == "resume":
            sub.add_argument(
                "--reconcile",
                action="store_true",
                help="Accept inspected workspace changes, invalidate evidence, restart planning",
            )
            sub.add_argument(
                "--feedback-file",
                help="Additional user guidance; cannot change frozen acceptance criteria",
            )
            sub.add_argument("--budget-usd", type=float, help="Explicitly replace the run budget")
            sub.add_argument(
                "--max-model-calls", type=int, help="Explicitly replace the total call limit"
            )
            sub.add_argument("--additional-seconds", type=int, default=0)
    return parser


def doctor(project: Path) -> int:
    settings = load_settings(project / "goodhands.toml")
    problems = []
    print(f"Python: {sys.version.split()[0]}")
    print(f"Provider: {settings.provider.base_url}")
    present = bool(os.environ.get(settings.provider.api_key_env))
    print(f"{settings.provider.api_key_env}: {'set (value hidden)' if present else 'not set'}")
    if not present:
        problems.append("API key is not set. Offline demo still works.")
    if any(p.model == "SET_MODEL_ID" for p in settings.profiles.values()):
        problems.append("Choose model IDs in goodhands.toml.")
    if not settings.checks:
        problems.append("Configure acceptance checks.")
    if settings.runner.kind == "docker":
        if not shutil.which("docker"):
            problems.append("Docker executable not found.")
        else:
            try:
                info = subprocess.run(
                    ["docker", "image", "inspect", settings.runner.image],
                    capture_output=True,
                    timeout=15,
                )
                if info.returncode:
                    problems.append(
                        f"Docker engine unavailable or image missing: {settings.runner.image}"
                    )
            except subprocess.TimeoutExpired:
                problems.append("Docker did not respond within 15 seconds.")
    else:
        print("Runner: local; live runs require --allow-local-exec.")
    for problem in problems:
        print("- " + problem)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = None
    try:
        if args.command == "init":
            project = Path(args.project).resolve()
            project.mkdir(parents=True, exist_ok=True)
            for filename in ("goodhands.toml", "task.json"):
                if (project / filename).exists():
                    raise ValueError(f"Refusing to overwrite {filename}")
            (project / "goodhands.toml").write_text(
                config_template(args.provider, args.model, args.runner), encoding="utf-8"
            )
            (project / "task.json").write_text(
                sample_task().model_dump_json(indent=2), encoding="utf-8"
            )
            print(
                "Created goodhands.toml and task.json. Edit the sample task, paths, checks and model before a live run."
            )
            return 0
        if args.command == "demo":
            directory = Path(
                args.directory or (Path(".goodhands") / "demos" / uuid.uuid4().hex[:12])
            ).resolve()
            create_demo(directory)
            store = Store(directory)
            settings = Settings(
                runner=RunnerSettings(kind="local"),
                checks={
                    "unit": Check(
                        argv=["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
                    )
                },
                policy=Policy(max_model_calls=100),
            )
            engine = Engine(
                store,
                DemoProvider(3 if args.exercise_recovery else 0),
                allow_local=True,
                progress=print,
            )
            with project_lock(store.root):
                state = engine.create(sample_task(), settings)
                state = engine.execute(state["id"])
            print_state(state, store)
            print(
                "Original calculator.py is unchanged. Inspect with diff; apply is a separate command."
            )
            return 0 if state["status"] == "completed_simulated" else 2
        project = Path(args.project).resolve()
        if args.command == "doctor":
            return doctor(project)
        store = Store(project)
        if args.command == "runs":
            for run in store.list():
                print(
                    f"{run['id']}  {run['status']:22}  ${run['charged_usd']:.4f}  {run['task']['title']}"
                )
            return 0
        if args.command == "show":
            print_state(store.get(args.run_id), store)
            return 0
        if args.command == "diff":
            store.get(args.run_id)
            home = store.home(args.run_id)
            print(diff_text(home / "base", home / "workspace"))
            return 0
        with project_lock(store.root):
            if args.command == "apply":
                state = store.get(args.run_id)
                if state["status"] not in {"completed", "completed_simulated"}:
                    raise ValueError("Only completed, verified workflow patches may be applied")
                home = store.home(args.run_id)
                if fingerprint(home / "workspace") != state["snapshot"]:
                    raise ValueError(
                        "Workspace changed after verification; patch is not applicable"
                    )
                changes = apply_changes(
                    project, home / "base", home / "workspace", home / "apply-backup"
                )
                state["status"] = "applied"
                store.save(state)
                print(
                    json.dumps({"applied": changes, "backup": str(home / "apply-backup")}, indent=2)
                )
                return 0
            if args.command == "run":
                settings = load_settings(project / "goodhands.toml")
                task_path = (project / args.task).resolve()
                task = load_task(task_path, settings)
                try:
                    # Keep the user-owned task immutable even with a broad editable glob.
                    task.protected_paths.append(task_path.relative_to(project).as_posix())
                except ValueError:
                    pass
                engine = Engine(store, allow_local=args.allow_local_exec, progress=print)
                state = engine.create(task, settings, args.preset, args.role)
                state = engine.execute(state["id"])
            else:
                state = store.get(args.run_id)
                if state["simulated"]:
                    raise ValueError(
                        "Demo runs are deterministic fixtures; start a new demo instead of resuming with a live provider"
                    )
                settings = Settings.model_validate(state["settings"])
                changes = {}
                if args.budget_usd is not None:
                    changes["budget_usd"] = args.budget_usd
                if args.max_model_calls is not None:
                    changes["max_model_calls"] = args.max_model_calls
                if args.additional_seconds < 0:
                    raise ValueError("additional-seconds cannot be negative")
                if args.additional_seconds:
                    changes["max_run_seconds"] = (
                        settings.policy.max_run_seconds + args.additional_seconds
                    )
                settings.policy = Policy.model_validate({**settings.policy.model_dump(), **changes})
                state["settings"] = settings.model_dump()
                store.save(state)
                feedback = (
                    Path(args.feedback_file).read_text("utf-8") if args.feedback_file else None
                )
                engine = Engine(store, allow_local=args.allow_local_exec, progress=print)
                state = engine.execute(state["id"], reconcile=args.reconcile, feedback=feedback)
            print_state(state, store)
            return (
                0 if state["status"] in {"completed", "completed_simulated", "report_ready"} else 2
            )
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
