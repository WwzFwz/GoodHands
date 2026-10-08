"""SQLite checkpoints plus immutable JSON artifacts and event logs."""

import contextlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def project_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "execution.lock").open("a+b")
    handle.seek(0, 2)
    if not handle.tell():
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("Another GoodHands operation is active for this project") from None
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class Store:
    def __init__(self, project: Path):
        from .workspace import linked

        self.project = project.resolve()
        self.root = self.project / ".goodhands"
        if self.root.exists() and linked(self.root):
            raise ValueError("State directory cannot be a symlink or junction")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.db")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def home(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{16}", run_id):
            raise ValueError("Invalid run ID")
        return self.root / "runs" / run_id

    def save(self, state: dict):
        state["updated_at"] = time.time()
        payload = json.dumps(state, ensure_ascii=False)
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)", (state["id"], payload))
        self.db.commit()

    def get(self, run_id: str) -> dict:
        self.home(run_id)
        row = self.db.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise ValueError(f"Run not found: {run_id}")
        return json.loads(row[0])

    def list(self) -> list[dict]:
        return sorted(
            (json.loads(row[0]) for row in self.db.execute("SELECT data FROM runs")),
            key=lambda r: r["created_at"],
            reverse=True,
        )

    def event(self, run_id: str, kind: str, **details):
        path = self.home(run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"time": time.time(), "type": kind, **details}, ensure_ascii=False)
                + "\n"
            )

    def artifact(self, run_id: str, kind: str, data: dict) -> str:
        artifact_id = uuid.uuid4().hex[:16]
        atomic_json(
            self.home(run_id) / "artifacts" / f"{artifact_id}.json",
            {"id": artifact_id, "kind": kind, **data},
        )
        return artifact_id

    def read_artifact(self, run_id: str, artifact_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{16}", artifact_id):
            raise ValueError("Invalid artifact ID")
        return json.loads(
            (self.home(run_id) / "artifacts" / f"{artifact_id}.json").read_text("utf-8")
        )
