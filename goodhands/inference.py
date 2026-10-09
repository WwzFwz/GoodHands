"""Shared inference limits and durable accounting for engineering and configuration."""

import json
import math
import time

from .models import Profile


class Halt(RuntimeError):
    """Retain a checkpoint instead of claiming completion."""


class BudgetedRequests:
    def __init__(self, store, state, settings, provider):
        self.store, self.state, self.settings, self.provider = store, state, settings, provider

    def request(
        self, stage: str, profile: Profile, messages: list[dict], schemas: list[dict]
    ) -> dict:
        policy = self.settings.policy
        if self.state["model_calls"] >= policy.max_model_calls:
            raise Halt("Model call limit reached")
        if (
            time.time() - self.state["session_started"] + self.state["elapsed_seconds"]
            >= policy.max_run_seconds
        ):
            raise Halt("Run time limit reached")
        if (
            len(json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False))
            > policy.max_context_chars
        ):
            raise Halt("Context budget exceeded; narrow the task or increase max_context_chars")
        reserve = 0 if self.provider.simulated else profile.request_reserve_usd
        if self.state["charged_usd"] + reserve > policy.budget_usd + 1e-9:
            raise Halt("Budget insufficient for the next request reservation")
        self.state["charged_usd"] += reserve
        self.state["model_calls"] += 1
        request_number = self.state["model_calls"]
        self.state["pending_request"] = {
            "number": request_number,
            "stage": stage,
            "reserve_usd": reserve,
        }
        self.store.save(self.state)
        self.store.event(
            self.state["id"],
            "model_request",
            number=request_number,
            stage=stage,
            model=profile.model,
            reserved_usd=reserve,
        )
        data = self.provider.complete(
            profile=profile, messages=messages, tools=schemas, stage=stage
        )
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        kind = "reported"
        if (
            not isinstance(cost, (int, float))
            or isinstance(cost, bool)
            or cost < 0
            or not math.isfinite(cost)
        ):
            cost = None
        if (
            cost is None
            and profile.input_usd_per_million is not None
            and profile.output_usd_per_million is not None
        ):
            incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if (
                isinstance(incoming, int)
                and incoming >= 0
                and isinstance(outgoing, int)
                and outgoing >= 0
            ):
                cost = (
                    incoming * profile.input_usd_per_million
                    + outgoing * profile.output_usd_per_million
                ) / 1_000_000
                kind = "estimated"
        if self.provider.simulated:
            cost, kind = 0, "simulated"
        if cost is not None:
            self.state["charged_usd"] += cost - reserve
        else:
            kind = "unknown_reserved"
        self.store.event(
            self.state["id"],
            "model_usage",
            number=request_number,
            model=data.get("model", profile.model),
            provider=data.get("provider"),
            usage=usage,
            cost_usd=cost,
            accounting=kind,
        )
        self.state["cost_accounting"][kind] = self.state["cost_accounting"].get(kind, 0) + 1
        self.state["pending_request"] = None
        self.store.save(self.state)
        if self.state["charged_usd"] > policy.budget_usd:
            raise Halt("Actual cost exceeded budget; no more actions will execute")
        if (
            time.time() - self.state["session_started"] + self.state["elapsed_seconds"]
            >= policy.max_run_seconds
        ):
            raise Halt(
                "Run time limit reached during inference; response accounted, tools not executed"
            )
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict) or not isinstance(message.get("tool_calls", []), list):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise Halt("Malformed provider message; checkpoint retained") from None
        return message
