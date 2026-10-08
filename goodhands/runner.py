"""Run only user-configured commands; never accept a model-generated shell string."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .models import Check, RunnerSettings


def terminate_tree(process: subprocess.Popen):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


class Runner:
    def __init__(self, settings: RunnerSettings, allow_local: bool = False):
        self.settings = settings
        self.allow_local = allow_local

    def preflight(self):
        if self.settings.kind == "local":
            if not self.allow_local:
                raise ValueError(
                    "Local execution requires --allow-local-exec for a trusted project. Otherwise configure Docker."
                )
        else:
            if not shutil.which("docker"):
                raise ValueError(
                    "Docker is not installed. Start Docker or explicitly use a trusted local runner."
                )
            result = subprocess.run(
                ["docker", "image", "inspect", self.settings.image], capture_output=True, timeout=15
            )
            if result.returncode:
                raise ValueError(
                    f"Docker unavailable or image missing. Start Docker and pull {self.settings.image} yourself."
                )

    def run(self, workspace: Path, check: Check, timeout: float | None = None) -> dict:
        self.preflight()
        started = time.monotonic()
        container = "goodhands-" + uuid.uuid4().hex[:12]
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper()
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "LANG",
            }
        }
        env.update(
            {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1"}
        )
        command = list(check.argv)
        if self.settings.kind == "docker":
            command = [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--memory",
                self.settings.memory,
                "--cpus",
                str(self.settings.cpus),
                "--mount",
                f"type=bind,source={workspace.resolve()},target=/workspace,readonly",
                "--tmpfs",
                "/tmp:rw,nosuid,size=64m",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-w",
                "/workspace",
                self.settings.image,
                *command,
            ]
        elif command[0] in {"python", "python3"}:
            command[0] = sys.executable
        if os.name == "nt" and command[0].lower().endswith((".bat", ".cmd")):
            raise ValueError("Batch wrappers are not supported; configure the executable directly")
        timed_out = False
        output_limited = False
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + (
                    min(check.timeout_seconds, timeout) if timeout else check.timeout_seconds
                )
                while process.poll() is None:
                    if os.fstat(output.fileno()).st_size > 1_000_000:
                        output_limited = True
                        terminate_tree(process)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        terminate_tree(process)
                        break
                    time.sleep(0.02)
            except BaseException:
                terminate_tree(process)
                raise
            finally:
                if self.settings.kind == "docker":
                    subprocess.run(
                        ["docker", "rm", "-f", container], capture_output=True, timeout=15
                    )
            output.seek(0)
            raw = output.read(64001)
            output_limited = output_limited or os.fstat(output.fileno()).st_size > 1_000_000
        return {
            "argv": check.argv,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output_limited": output_limited,
            "output": raw[:64000].decode("utf-8", errors="replace"),
            "output_truncated": len(raw) > 64000,
            "duration_seconds": round(time.monotonic() - started, 3),
            "runner": self.settings.kind,
            "image": self.settings.image if self.settings.kind == "docker" else None,
        }
