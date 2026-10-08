"""Role-scoped file tools and named checks with durable execution evidence."""

import json
import os
import time
import uuid

from pydantic import Field

from .models import Contract, Settings, Task
from .runner import Runner
from .store import Store
from .workspace import digest, fingerprint, inventory, matches, safe_path


class ListArgs(Contract):
    prefix: str = ""


class ReadArgs(Contract):
    path: str
    start_line: int = Field(default=1, ge=1)
    line_count: int = Field(default=120, ge=1, le=250)


class SearchArgs(Contract):
    text: str = Field(min_length=1, max_length=200)


class WriteArgs(Contract):
    path: str
    content: str = Field(max_length=128000)
    expected_sha256: str | None


class DeleteArgs(Contract):
    path: str
    expected_sha256: str


class CheckArgs(Contract):
    check_id: str


TOOL_ARGS = {
    "list_files": ListArgs,
    "read_file": ReadArgs,
    "search_text": SearchArgs,
    "write_file": WriteArgs,
    "delete_file": DeleteArgs,
    "run_check": CheckArgs,
}
DESCRIPTIONS = {
    "list_files": "List accessible repository paths, optionally filtered by prefix.",
    "read_file": "Read UTF-8 lines and the full file sha256. Use the hash when editing.",
    "search_text": "Search literal text in repository files; returns bounded matches.",
    "write_file": "Write a permitted UTF-8 file. Existing files require the hash from read_file; new files require null.",
    "delete_file": "Delete a permitted product file using its current sha256.",
    "run_check": "Execute a user-configured check ID. Arbitrary commands are not accepted. Returns actual evidence ID.",
}


def tool_schema(name: str, schema: type[Contract], description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema.model_json_schema(),
        },
    }


class ToolExecutor:
    def __init__(
        self, store: Store, state: dict, settings: Settings, task: Task, allow_local: bool
    ):
        self.store, self.state, self.settings, self.task = store, state, settings, task
        self.workspace = store.home(state["id"]) / "workspace"
        self.base = store.home(state["id"]) / "base"
        self.runner = Runner(settings.runner, allow_local)

    def permitted_write(self, role: str, relative: str):
        path = safe_path(self.workspace, relative)
        relative = path.relative_to(self.workspace).as_posix()
        if role == "tester":
            allowed = (
                matches(relative, self.task.test_paths) and not (self.base / relative).exists()
            )
        elif role == "documentor":
            allowed = matches(relative, self.task.documentation_paths) and not matches(
                relative, self.task.protected_paths + self.task.test_paths
            )
        else:
            allowed = (
                role == "coder"
                and matches(relative, self.task.editable_paths)
                and not matches(relative, self.task.protected_paths + self.task.test_paths)
            )
        if not allowed:
            raise ValueError(f"{role} cannot modify {relative}")
        return path

    def execute(self, role: str, allowed: set[str], name: str, arguments: dict) -> dict:
        if name not in allowed or name not in TOOL_ARGS:
            raise ValueError(f"Tool {name} is not permitted for {role}")
        args = TOOL_ARGS[name].model_validate(arguments)
        execution_id = uuid.uuid4().hex[:16]
        metadata = args.model_dump(exclude={"content"})
        pending = {
            "id": execution_id,
            "tool": name,
            "role": role,
            "status": "running",
            "arguments": metadata,
        }
        self.state["pending_tool"] = pending
        self.store.save(self.state)
        self.store.event(self.state["id"], "tool_started", **pending)
        try:
            result = self._execute(role, name, args)
            self.state["snapshot"] = fingerprint(self.workspace)
        except Exception:
            self.state["snapshot"] = fingerprint(self.workspace)
            self.state["pending_tool"] = None
            self.store.save(self.state)
            self.store.event(self.state["id"], "tool_failed", execution_id=execution_id, tool=name)
            raise
        self.state["pending_tool"] = None
        self.store.save(self.state)
        self.store.event(
            self.state["id"],
            "tool_finished",
            execution_id=execution_id,
            tool=name,
            snapshot=self.state["snapshot"],
        )
        return result

    def _execute(self, role: str, name: str, args: Contract) -> dict:
        if name == "list_files":
            paths = [p for p in inventory(self.workspace) if p.startswith(args.prefix)]
            return {"paths": paths[:500], "truncated": len(paths) > 500}
        if name == "read_file":
            path = safe_path(self.workspace, args.path)
            lines = path.read_text("utf-8").splitlines()
            selected = lines[args.start_line - 1 : args.start_line - 1 + args.line_count]
            return {
                "path": args.path,
                "sha256": digest(path),
                "total_lines": len(lines),
                "content": "\n".join(
                    f"{i}: {line}" for i, line in enumerate(selected, args.start_line)
                )[:24000],
            }
        if name == "search_text":
            found = []
            for relative in inventory(self.workspace):
                try:
                    content = safe_path(self.workspace, relative).read_text("utf-8")
                except UnicodeError:
                    continue
                for number, line in enumerate(content.splitlines(), 1):
                    if args.text in line:
                        found.append({"path": relative, "line": number, "text": line[:400]})
                        if len(found) >= 60:
                            return {"matches": found, "truncated": True}
            return {"matches": found, "truncated": False}
        if name in {"write_file", "delete_file"}:
            path = self.permitted_write(role, args.path)
            actual = digest(path) if path.is_file() else None
            if actual != args.expected_sha256:
                raise ValueError("Stale file hash; read the current file before editing")
            if name == "delete_file":
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Temporary files stay in the same directory for atomic replacement.
                temporary = path.with_name(path.name + ".goodhands-tmp")
                if temporary.exists():
                    raise ValueError("Temporary path already exists")
                try:
                    with temporary.open("x", encoding="utf-8", newline="") as handle:
                        handle.write(args.content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary.replace(path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            self.state["evidence"] = {}
            return {"path": args.path, "sha256": digest(path) if path.exists() else None}
        if name == "run_check":
            return self.run_check(args.check_id)
        raise ValueError("Unknown tool")

    def run_check(self, check_id: str) -> dict:
        if check_id not in self.settings.checks:
            raise ValueError("Unknown check ID")
        before = fingerprint(self.workspace)
        remaining = self.settings.policy.max_run_seconds - (
            time.time() - self.state["session_started"] + self.state["elapsed_seconds"]
        )
        if remaining <= 0:
            raise ValueError("Run time budget exhausted")
        result = self.runner.run(self.workspace, self.settings.checks[check_id], remaining)
        after = fingerprint(self.workspace)
        result.update(
            {
                "check_id": check_id,
                "snapshot": after,
                "workspace_mutated": before != after,
                "passed": result["exit_code"] == 0
                and not result["timed_out"]
                and not result["output_limited"]
                and before == after,
                "simulated_model": self.state["simulated"],
            }
        )
        artifact_id = self.store.artifact(self.state["id"], "verification", result)
        self.state["evidence"][check_id] = artifact_id
        self.store.save(self.state)
        return {"evidence_id": artifact_id, **result, "output": result["output"][:12000]}


def json_result(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)
