"""Validated contracts shared by providers, tools, and workflow stages."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROLES = (
    "consultant",
    "system_architect",
    "code_architect",
    "coder",
    "tester",
    "reviewer",
    "debugger",
    "documentor",
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Criterion(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(min_length=1)
    check_ids: list[str] = Field(min_length=1)


class Task(Contract):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    criteria: list[Criterion] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    editable_paths: list[str] = Field(min_length=1)
    protected_paths: list[str] = Field(default_factory=lambda: ["tests/**"])
    test_paths: list[str] = Field(default_factory=lambda: ["tests/generated/**"])
    documentation_paths: list[str] = Field(default_factory=lambda: ["docs/**"])
    risk: Literal["low", "medium", "high"] = "medium"
    complexity: Literal["simple", "complex"] = "simple"
    architecture_required: bool = False
    document: bool = False
    skills: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_criteria(self):
        ids = [c.id for c in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Criterion IDs must be unique")
        return self


class Check(Contract):
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=60, ge=1, le=1800)


class Profile(Contract):
    model: str = "SET_MODEL_ID"
    max_output_tokens: int = Field(default=4096, ge=256, le=65536)
    # Conservative user-supplied reservation; actual cost is reconciled afterward.
    request_reserve_usd: float = Field(default=0.10, gt=0)
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)


class ProviderSettings(Contract):
    kind: Literal["openrouter", "compatible"] = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout_seconds: int = Field(default=90, ge=1, le=600)


class Policy(Contract):
    budget_usd: float = Field(default=2.0, gt=0)
    max_model_calls: int = Field(default=50, ge=1, le=1000)
    max_role_turns: int = Field(default=12, ge=1, le=100)
    max_repairs: int = Field(default=2, ge=0, le=10)
    max_run_seconds: int = Field(default=1800, ge=1, le=86400)
    max_context_chars: int = Field(default=60000, ge=4000, le=1000000)
    reviewer: Literal["risk", "always"] = "risk"
    escalation_profile: str | None = None


class RunnerSettings(Contract):
    kind: Literal["docker", "local"] = "docker"
    image: str = "python:3.12-slim"
    memory: str = "512m"
    cpus: float = Field(default=1.0, gt=0, le=16)


class Settings(Contract):
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    policy: Policy = Field(default_factory=Policy)
    runner: RunnerSettings = Field(default_factory=RunnerSettings)
    profiles: dict[str, Profile] = Field(default_factory=lambda: {"default": Profile()})
    role_profiles: dict[str, str] = Field(default_factory=dict)
    checks: dict[str, Check] = Field(default_factory=dict)
    roles_dir: str | None = None
    skills_dir: str | None = None

    @model_validator(mode="after")
    def references(self):
        if "default" not in self.profiles:
            raise ValueError("profiles.default is required")
        for role, profile in self.role_profiles.items():
            if role not in ROLES or profile not in self.profiles:
                raise ValueError(f"Invalid role/profile mapping: {role} -> {profile}")
        if self.policy.escalation_profile and self.policy.escalation_profile not in self.profiles:
            raise ValueError("Escalation profile does not exist")
        for name in self.checks:
            if not name or not all(c.isalnum() or c in "_-" for c in name):
                raise ValueError("Check IDs must be letters, digits, underscores or hyphens")
        return self


class Finding(Contract):
    severity: Literal["blocking", "suggestion"]
    location: str
    evidence: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


class Reference(Contract):
    path: str
    reason: str


class Result(Contract):
    summary: str = Field(min_length=1)
    status: Literal["ready", "revise", "blocked"]
    next_owner: Literal["coder", "code_architect", "system_architect", "consultant", "user"] = (
        "coder"
    )
    findings: list[Finding] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)


class ContextResult(Result):
    shared_context: str = Field(min_length=1)
    role_context: dict[str, str] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)


class TechnicalContract(Contract):
    requirement_ids: list[str] = Field(min_length=1)
    files: list[str] = Field(min_length=1)
    inputs: str
    outputs: str
    errors: list[str]
    invariants: list[str]


class PlanResult(Result):
    contracts: list[TechnicalContract] = Field(min_length=1)


class ArchitectureResult(Result):
    decisions: list[str] = Field(min_length=1)
    alternatives: list[str] = Field(min_length=1)
    tradeoffs: list[str] = Field(min_length=1)


class TestPlanResult(Result):
    # Keys refer to user-owned criteria; values to user-configured check IDs.
    verification_plan: dict[str, list[str]]
    edge_cases: list[str] = Field(default_factory=list)


class TestResult(Result):
    evidence_ids: list[str] = Field(min_length=1)


class DiagnosisResult(Result):
    reproduction: str = Field(min_length=1)
    hypotheses_tested: list[str] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    suggested_fix: str = Field(min_length=1)


RESULT_TYPES = {
    "consultant": ContextResult,
    "system_architect": ArchitectureResult,
    "code_architect": PlanResult,
    "tester_plan": TestPlanResult,
    "tester": TestResult,
    "debugger": DiagnosisResult,
}


def result_type(stage: str) -> type[Result]:
    return RESULT_TYPES.get(stage, Result)
