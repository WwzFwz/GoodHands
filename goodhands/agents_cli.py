"""Configuration commands kept separate from engineering workflow commands."""

import json
import uuid
from pathlib import Path

from .config import load_settings
from .configurator import apply_configuration, configure
from .models import ROLES, Settings
from .registry import compile_registry, scaffold, write_compiled
from .store import Store, project_lock
from .workspace import diff_text


def add_agents_parser(commands):
    parser = commands.add_parser(
        "agents", help="Author roles with Markdown or a dedicated Configurator"
    )
    actions = parser.add_subparsers(dest="agents_command", required=True)
    for command in ("init", "validate", "compile", "configure", "diff", "apply", "demo"):
        sub = actions.add_parser(command)
        sub.add_argument("--project", default=".")
        if command in {"diff", "apply"}:
            sub.add_argument("run_id")
        if command == "configure":
            sub.add_argument("--role", required=True, choices=ROLES)
            request = sub.add_mutually_exclusive_group(required=True)
            request.add_argument(
                "--request", help="Describe the desired role/skill in ordinary language"
            )
            request.add_argument(
                "--request-file", help="Read ordinary-language instructions from UTF-8 text"
            )
            sub.add_argument("--profile", help="Override the configurator model profile")
            sub.add_argument(
                "--apply", action="store_true", help="Apply the validated draft immediately"
            )


def agents_main(args) -> int:
    project = Path(args.project).resolve()
    command = args.agents_command
    if command == "demo":
        from .demo import ConfigurationDemoProvider

        project = project / ".goodhands" / "config-demos" / uuid.uuid4().hex[:12]
        project.mkdir(parents=True)
        settings = Settings()
    else:
        settings_path = project / "goodhands.toml"
        settings = load_settings(settings_path) if settings_path.is_file() else Settings()
    if command == "validate":
        manifest = compile_registry(project, settings)
        print(f"Valid: {len(manifest['roles'])} roles; all included skills resolved.")
        return 0
    store = Store(project)
    try:
        with project_lock(store.root):
            if command == "init":
                print(json.dumps({"created": scaffold(project, settings)}, indent=2))
            elif command == "compile":
                print(write_compiled(project, settings, store.root))
            elif command == "diff":
                state = store.get(args.run_id)
                if state.get("kind") != "configuration":
                    raise ValueError("Expected a configuration run")
                home = store.home(args.run_id)
                print(diff_text(home / "base", home / "workspace"))
            elif command == "apply":
                print(json.dumps({"applied": apply_configuration(store, args.run_id)}, indent=2))
            else:
                if command == "demo":
                    state = configure(
                        store,
                        settings,
                        "coder",
                        "Ikuti kontrak dan hindari abstraksi berlebihan.",
                        provider=ConfigurationDemoProvider(),
                    )
                else:
                    if not (project / "goodhands.toml").is_file():
                        raise ValueError(
                            "Run goodhands init and choose a model before using live Configurator"
                        )
                    request = args.request
                    if args.request_file:
                        path = Path(args.request_file)
                        if path.stat().st_size > 64000:
                            raise ValueError("Request file too large")
                        request = path.read_text("utf-8-sig")
                    state = configure(
                        store, settings, args.role, request, profile_name=args.profile
                    )
                print(
                    json.dumps(
                        {
                            key: state[key]
                            for key in (
                                "id",
                                "status",
                                "message",
                                "model_calls",
                                "charged_usd",
                                "simulated",
                            )
                        }
                        | {"project": str(project), "questions": state.get("questions", [])},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                if state["status"] != "config_ready":
                    return 2
                print(
                    diff_text(
                        store.home(state["id"]) / "base", store.home(state["id"]) / "workspace"
                    )
                )
                if command == "configure" and args.apply:
                    print(
                        json.dumps({"applied": apply_configuration(store, state["id"])}, indent=2)
                    )
                else:
                    print(f'Apply: goodhands agents apply {state["id"]} --project "{project}"')
        return 0
    finally:
        store.close()
