"""A sequential controller owns transitions; agents cannot waive acceptance checks."""

import json
import time
import uuid

from .config import Role, load_roles, load_skills
from .models import ROLES, Settings, Task
from .provider import ChatProvider, Provider
from .runtime import Halt, Runtime, role_for
from .store import Store
from .tools import ToolExecutor
from .workspace import changed_files, copy_snapshot, fingerprint, inventory, matches


class Engine:
    def __init__(
        self,
        store: Store,
        provider: Provider | None = None,
        allow_local: bool = False,
        progress=None,
    ):
        self.store = store
        self.provider = provider
        self.allow_local = allow_local
        self.progress = progress or (lambda text: None)

    @staticmethod
    def review_required(task: Task, settings: Settings) -> bool:
        return settings.policy.reviewer == "always" or task.risk != "low"

    def implementation_steps(self, task: Task, settings: Settings, preset: str) -> list[str]:
        steps = ["coder", "verify"]
        if preset != "quick":
            steps.append("tester")
        if self.review_required(task, settings):
            steps.append("reviewer")
        if task.document:
            steps.append("documentor")
        return steps + ["final_verify", "complete"]

    def planning_steps(self, task: Task, settings: Settings, preset: str) -> list[str]:
        steps = ["consultant"]
        if task.architecture_required or preset == "system":
            steps.append("system_architect")
        if preset != "quick":
            steps.append("code_architect")
            if task.complexity == "complex":
                steps.append("tester_plan")
            if task.risk == "high" or settings.policy.reviewer == "always":
                steps.append("plan_review")
        return steps

    def create(
        self, task: Task, settings: Settings, preset: str = "feature", mode: str | None = None
    ) -> dict:
        if preset not in {"quick", "feature", "system"} or mode is not None and mode not in ROLES:
            raise ValueError("Unknown preset or role")
        for criterion in task.criteria:
            if not set(criterion.check_ids) <= settings.checks.keys():
                raise ValueError("Acceptance criteria reference unconfigured checks")
        roles = load_roles(self.store.project, settings)
        skill_texts = {
            name: load_skills(self.store.project, settings, task.skills + role.skills)
            for name, role in roles.items()
        }
        steps = (
            (["verify", "tester"] if mode == "tester" else [mode])
            if mode
            else self.planning_steps(task, settings, preset)
            + self.implementation_steps(task, settings, preset)
        )
        for stage in steps:
            name = role_for(stage)
            if name in roles and not roles[name].enabled:
                raise ValueError(f"Required role is disabled: {name}")
        provider = self.provider or ChatProvider(settings.provider)
        if not provider.simulated:
            if isinstance(provider, ChatProvider):
                provider.preflight()
            for profile in settings.profiles.values():
                if not profile.model.strip() or profile.model == "SET_MODEL_ID":
                    raise ValueError("Set model IDs in goodhands.toml first")
        # Verify executable availability before incurring inference costs.
        if not mode or mode == "tester":
            from .runner import Runner

            Runner(settings.runner, self.allow_local).preflight()
        run_id = uuid.uuid4().hex[:16]
        home = self.store.home(run_id)
        home.mkdir(parents=True)
        copy_snapshot(self.store.project, home / "base")
        copy_snapshot(home / "base", home / "workspace")
        state = {
            "id": run_id,
            "created_at": time.time(),
            "status": "created",
            "message": "",
            "task": task.model_dump(),
            "settings": settings.model_dump(),
            "preset": preset,
            "mode": mode,
            "roles": {name: role.model_dump() for name, role in roles.items()},
            "skills": skill_texts,
            "steps": steps,
            "results": {},
            "evidence": {},
            "history": [],
            "repairs": 0,
            "debugger_used": False,
            "escalated": False,
            "model_calls": 0,
            "charged_usd": 0.0,
            "cost_accounting": {},
            "elapsed_seconds": 0.0,
            "session_started": time.time(),
            "pending_tool": None,
            "pending_request": None,
            "simulated": provider.simulated,
            "snapshot": fingerprint(home / "workspace"),
        }
        self.store.save(state)
        self.store.event(run_id, "created", preset=preset, mode=mode, simulated=provider.simulated)
        return state

    def evidence_valid(self, state: dict, task: Task, snapshot: str) -> bool:
        for check_id in {i for criterion in task.criteria for i in criterion.check_ids}:
            evidence_id = state["evidence"].get(check_id)
            if not evidence_id:
                return False
            evidence = self.store.read_artifact(state["id"], evidence_id)
            if not evidence["passed"] or evidence["snapshot"] != snapshot:
                return False
        return True

    def verify(self, state: dict, task: Task, executor: ToolExecutor) -> bool:
        for check_id in sorted({i for c in task.criteria for i in c.check_ids}):
            # Always use the same durable tool path as model-triggered checks.
            executor.execute("tester", {"run_check"}, "run_check", {"check_id": check_id})
        return self.evidence_valid(state, task, fingerprint(executor.workspace))

    def integrity(self, task: Task, executor: ToolExecutor):
        baseline = inventory(executor.base)
        for relative in changed_files(executor.base, executor.workspace):
            is_new_test = relative not in baseline and matches(relative, task.test_paths)
            product_or_doc = matches(relative, task.editable_paths + task.documentation_paths)
            if not is_new_test and (
                not product_or_doc or matches(relative, task.protected_paths + task.test_paths)
            ):
                raise Halt(f"Unauthorized or protected change detected: {relative}")

    def repair(self, state: dict, task: Task, settings: Settings, owner: str, feedback: str):
        state["feedback"] = feedback
        if state["mode"]:
            raise Halt("Standalone role requires follow-up: " + feedback[:500])
        if owner == "user":
            raise Halt("User decision required: " + feedback[:500])
        if state["repairs"] >= settings.policy.max_repairs:
            if not state["debugger_used"]:
                state["debugger_used"] = True
                if settings.policy.escalation_profile:
                    state["escalated"] = True
                state["steps"] = ["debugger"]
                return
            raise Halt("Repair and diagnosis limit reached; review the saved evidence")
        state["repairs"] += 1
        self.route_repair(state, task, settings, owner)

    def route_repair(self, state: dict, task: Task, settings: Settings, owner: str):
        # Refresh source context after any patch before another implementation attempt.
        steps = ["consultant"]
        if owner == "system_architect":
            steps += ["system_architect", "code_architect"]
        elif owner == "code_architect":
            steps += ["code_architect"]
        if "code_architect" in steps:
            if task.complexity == "complex":
                steps.append("tester_plan")
            if task.risk == "high" or settings.policy.reviewer == "always":
                steps.append("plan_review")
        state["steps"] = steps + self.implementation_steps(task, settings, state["preset"])

    def execute(self, run_id: str, *, reconcile: bool = False, feedback: str | None = None) -> dict:
        state = self.store.get(run_id)
        if state["status"] in {"completed", "completed_simulated", "report_ready", "applied"}:
            return state
        settings, task = (
            Settings.model_validate(state["settings"]),
            Task.model_validate(state["task"]),
        )
        provider = self.provider or ChatProvider(settings.provider)
        if bool(provider.simulated) != bool(state["simulated"]):
            raise ValueError("Cannot change simulated/live provider identity on resume")
        executor = ToolExecutor(self.store, state, settings, task, self.allow_local)
        current = fingerprint(executor.workspace)
        if state["pending_tool"] or current != state["snapshot"]:
            if not reconcile:
                raise ValueError(
                    "Workspace changed or tool outcome unknown. Inspect diff, then resume --reconcile to invalidate old evidence and restart planning."
                )
            state["evidence"], state["results"] = {}, {}
            state["pending_tool"] = None
            state["snapshot"] = current
            state["steps"] = (
                (["verify", "tester"] if state["mode"] == "tester" else [state["mode"]])
                if state["mode"]
                else self.planning_steps(task, settings, state["preset"])
                + self.implementation_steps(task, settings, state["preset"])
            )
            self.store.event(run_id, "reconciled", snapshot=current)
        if feedback:
            state["feedback"] = feedback
        if state.get("pending_request"):
            self.store.event(run_id, "request_outcome_unknown", **state["pending_request"])
            state["cost_accounting"]["unknown_reserved"] = (
                state["cost_accounting"].get("unknown_reserved", 0) + 1
            )
            state["pending_request"] = None
        runtime = Runtime(self.store, state, settings, task, provider, executor)
        state["status"], state["message"], state["session_started"] = "running", "", time.time()
        self.store.save(state)
        try:
            while state["steps"]:
                if (
                    time.time() - state["session_started"] + state["elapsed_seconds"]
                    >= settings.policy.max_run_seconds
                ):
                    raise Halt("Run time limit reached")
                stage = state["steps"][0]
                if not state["mode"] and stage not in {
                    "consultant",
                    "verify",
                    "final_verify",
                    "complete",
                    "debugger",
                }:
                    context_id = state["results"].get("consultant")
                    if context_id and self.store.read_artifact(run_id, context_id)[
                        "snapshot"
                    ] != fingerprint(executor.workspace):
                        state["steps"].insert(0, "consultant")
                        stage = "consultant"
                self.progress(f"[{run_id}] {stage}")
                self.store.event(run_id, "stage_started", stage=stage)
                self.integrity(task, executor)
                if stage in {"verify", "final_verify"}:
                    if not self.verify(state, task, executor):
                        failures = {
                            key: self.store.read_artifact(run_id, val)
                            for key, val in state["evidence"].items()
                        }
                        self.repair(
                            state,
                            task,
                            settings,
                            "coder",
                            json.dumps(failures, ensure_ascii=False)[-18000:],
                        )
                    else:
                        state["steps"].pop(0)
                        if stage == "final_verify" and self.review_required(task, settings):
                            artifact_id = state["results"].get("reviewer")
                            if (
                                not artifact_id
                                or self.store.read_artifact(run_id, artifact_id)["snapshot"]
                                != state["snapshot"]
                            ):
                                state["steps"].insert(0, "reviewer")
                elif stage == "complete":
                    self.integrity(task, executor)
                    if not self.evidence_valid(state, task, fingerprint(executor.workspace)):
                        raise Halt("Final gate lacks passing evidence on the final snapshot")
                    if self.review_required(task, settings):
                        review = self.store.read_artifact(run_id, state["results"]["reviewer"])
                        if review["snapshot"] != state["snapshot"]:
                            raise Halt("Reviewer evidence is stale")
                    state["status"] = "completed_simulated" if state["simulated"] else "completed"
                    state["message"] = "Verified patch is ready for diff and apply."
                    state["steps"].pop(0)
                else:
                    role = Role.model_validate(state["roles"][role_for(stage)])
                    if not role.enabled:
                        raise Halt(f"Required role is disabled: {role.name}")
                    result = runtime.run(stage, role, state["skills"][role.name])
                    state["snapshot"] = fingerprint(executor.workspace)
                    artifact_id = self.store.artifact(
                        run_id,
                        stage,
                        {
                            "result": result,
                            "snapshot": state["snapshot"],
                            "role_version": role.version,
                        },
                    )
                    state["results"][stage] = artifact_id
                    state["history"].append({"stage": stage, "artifact_id": artifact_id})
                    if state["mode"]:
                        state["steps"].pop(0)
                        if result["status"] != "ready" or any(
                            f["severity"] == "blocking" for f in result["findings"]
                        ):
                            state["steps"] = [stage]
                            raise Halt(result["summary"])
                    elif stage == "debugger":
                        if result["status"] == "blocked" or result["next_owner"] == "user":
                            raise Halt("Debugger needs a user decision: " + result["summary"])
                        state["feedback"] = json.dumps(result, ensure_ascii=False)
                        self.route_repair(state, task, settings, result["next_owner"])
                    elif result["status"] != "ready" or any(
                        f["severity"] == "blocking" for f in result["findings"]
                    ):
                        self.repair(
                            state,
                            task,
                            settings,
                            result["next_owner"],
                            json.dumps(result, ensure_ascii=False),
                        )
                    else:
                        state["steps"].pop(0)
                self.store.save(state)
            if state["mode"]:
                state["status"] = "report_ready"
                state["message"] = (
                    "Standalone role artifact ready; this is not a verified full workflow."
                )
        except KeyboardInterrupt:
            state["status"], state["message"] = (
                "interrupted",
                "Interrupted. Inspect state before resume.",
            )
        except Exception as error:
            state["status"], state["message"] = "blocked", f"{type(error).__name__}: {error}"
            self.store.event(run_id, "blocked", message=state["message"])
        finally:
            if state.get("pending_request"):
                self.store.event(run_id, "request_outcome_unknown", **state["pending_request"])
                state["cost_accounting"]["unknown_reserved"] = (
                    state["cost_accounting"].get("unknown_reserved", 0) + 1
                )
                state["pending_request"] = None
            state["elapsed_seconds"] += time.time() - state["session_started"]
            self.store.save(state)
            report = {
                k: state[k]
                for k in (
                    "id",
                    "status",
                    "message",
                    "simulated",
                    "charged_usd",
                    "cost_accounting",
                    "model_calls",
                    "elapsed_seconds",
                    "repairs",
                    "debugger_used",
                    "snapshot",
                    "results",
                    "evidence",
                )
            }
            report_id = self.store.artifact(run_id, "completion", report)
            state["completion_artifact"] = report_id
            self.store.save(state)
        return state
