"""Deterministic simulator for exercising the harness, not a coding model."""

import json
import uuid
from pathlib import Path

from .models import Profile

CALCULATOR = "def add(a, b):\n    return a - b\n"
CORRECT_CALCULATOR = "def add(a, b):\n    return a + b\n"
TESTS = """import unittest
from calculator import add


class AdditionTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative(self):
        self.assertEqual(add(-2, -3), -5)

    def test_zero(self):
        self.assertEqual(add(4, 0), 4)


if __name__ == "__main__":
    unittest.main()
"""


class DemoProvider:
    simulated = True

    def __init__(self, fail_coder_attempts: int = 0):
        self.fail_coder_attempts = fail_coder_attempts
        self.coder_attempts = 0

    def complete(
        self, *, profile: Profile, messages: list[dict], tools: list[dict], stage: str
    ) -> dict:
        context = json.loads(messages[1]["content"])
        responses = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        result = {
            "summary": f"Simulated {stage} artifact; actual file tools and test runner are used.",
            "status": "ready",
            "references": [{"path": "calculator.py", "reason": "Implementation under test"}],
        }
        name, args = "submit_result", result
        if stage in {"consultant", "coder", "debugger"} and not responses:
            if stage == "coder":
                self.coder_attempts += 1
            name, args = "read_file", {"path": "calculator.py"}
        elif stage == "coder" and len(responses) == 1:
            name, args = (
                "write_file",
                {
                    "path": "calculator.py",
                    "expected_sha256": responses[0]["sha256"],
                    "content": CALCULATOR
                    if self.coder_attempts <= self.fail_coder_attempts
                    else CORRECT_CALCULATOR,
                },
            )
        elif stage == "consultant":
            result.update(
                shared_context="Small Python calculator with unittest acceptance checks.",
                role_context={
                    "coder": "Fix addition in calculator.py; preserve trusted tests.",
                    "tester": "Check positive, negative and zero inputs.",
                },
                uncertainties=[],
            )
        elif stage == "system_architect":
            result.update(
                decisions=["Keep a single pure function"],
                alternatives=["Class wrapper"],
                tradeoffs=["A class adds no value for this scope"],
            )
        elif stage == "code_architect":
            result["contracts"] = [
                {
                    "requirement_ids": [c["id"] for c in context["task"]["criteria"]],
                    "files": ["calculator.py"],
                    "inputs": "Two numbers",
                    "outputs": "Their sum",
                    "errors": [],
                    "invariants": ["Existing API remains unchanged"],
                }
            ]
        elif stage == "tester_plan":
            result.update(
                verification_plan={c["id"]: c["check_ids"] for c in context["task"]["criteria"]},
                edge_cases=["negative inputs", "zero"],
            )
        elif stage == "tester":
            result["evidence_ids"] = [item["id"] for item in context["evidence"].values()]
        elif stage == "debugger":
            result.update(
                reproduction="Run configured unit check",
                hypotheses_tested=[
                    "Function subtracts instead of adding, confirmed by source and failed assertions"
                ],
                evidence=["calculator.py uses subtraction; unit check fails"],
                suggested_fix="Replace subtraction with addition",
                next_owner="coder",
            )
        elif stage == "documentor" and not responses:
            name, args = (
                "write_file",
                {
                    "path": "docs/calculator.md",
                    "expected_sha256": None,
                    "content": "# Calculator\n\nThe add function returns the sum of two numbers.\n",
                },
            )
        return {
            "model": "simulator",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": uuid.uuid4().hex[:12],
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                        ],
                    }
                }
            ],
        }


def create_demo(directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "calculator.py").write_text(CALCULATOR, encoding="utf-8")
    (directory / "tests").mkdir()
    (directory / "tests" / "test_calculator.py").write_text(TESTS, encoding="utf-8")
