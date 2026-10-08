"""Bounded snapshots, guarded file access, and conflict-checked patch application."""

import difflib
import fnmatch
import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

EXCLUDED = {
    ".git",
    ".goodhands",
    ".goodhands-demo",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    "dist",
    "build",
    "goodhands.toml",
}
MAX_FILE_BYTES = 2_000_000
MAX_FILES = 10000
MAX_TOTAL_BYTES = 100_000_000


def hidden(path: str) -> bool:
    return any(
        part in EXCLUDED
        or part.startswith(".env")
        or part.endswith(".egg-info")
        or part.lower().endswith((".pem", ".key", ".pfx"))
        for part in (p.lower() for p in PurePosixPath(path).parts)
    )


def linked(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def safe_path(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not parts
        or normalized.startswith("/")
        or any(
            p in {".", ".."}
            or ":" in p
            or p.endswith((" ", "."))
            or p.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for p in parts
        )
        or hidden(normalized)
    ):
        raise ValueError("Path is outside allowed repository content")
    root = root.resolve()
    candidate = root.joinpath(*parts)
    candidate.resolve().relative_to(root)
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if linked(cursor):
                raise ValueError("Symlinks and junctions are not accessible")
    if candidate.is_file() and candidate.stat().st_nlink > 1:
        raise ValueError("Hard-linked files are not accessible")
    return candidate


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    result = {}
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(
            d
            for d in dirs
            if not hidden((parent / d).relative_to(root).as_posix()) and not linked(parent / d)
        )
        for name in sorted(names):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if hidden(relative) or linked(path):
                continue
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(result) >= MAX_FILES:
                raise ValueError("Repository exceeds snapshot limits; use a smaller project root")
            result[relative] = digest(path)
    return result


def fingerprint(root: Path) -> str:
    content = "\n".join(f"{p}:{h}" for p, h in sorted(inventory(root).items()))
    return hashlib.sha256(content.encode()).hexdigest()


def copy_snapshot(source: Path, target: Path) -> dict[str, str]:
    manifest = inventory(source)
    target.mkdir(parents=True, exist_ok=False)
    for relative in manifest:
        src = safe_path(source, relative)
        dst = target / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        # Preserve executable bits without propagating read-only attributes.
        if os.name != "nt":
            dst.chmod(src.stat().st_mode & 0o777)
        if digest(dst) != manifest[relative]:
            raise ValueError("Repository changed during snapshot creation; start a new run")
    return manifest


def matches(relative: str, patterns: list[str]) -> bool:
    if os.name == "nt":
        relative, patterns = relative.casefold(), [p.casefold() for p in patterns]
    return any(fnmatch.fnmatchcase(relative, pattern.replace("\\", "/")) for pattern in patterns)


def changed_files(base: Path, workspace: Path) -> list[str]:
    before, after = inventory(base), inventory(workspace)
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def diff_text(base: Path, workspace: Path) -> str:
    output = []
    for relative in changed_files(base, workspace):
        before, after = base / relative, workspace / relative
        try:
            old = (
                before.read_text(encoding="utf-8").splitlines(keepends=True)
                if before.exists()
                else []
            )
            new = (
                after.read_text(encoding="utf-8").splitlines(keepends=True)
                if after.exists()
                else []
            )
            output.extend(difflib.unified_diff(old, new, f"a/{relative}", f"b/{relative}"))
        except UnicodeError:
            output.append(f"Binary file changed: {relative}\n")
    return "".join(output)


def apply_changes(source: Path, base: Path, workspace: Path, backup: Path) -> list[str]:
    changes = changed_files(base, workspace)
    before = inventory(base)
    if inventory(source) != before:
        raise ValueError(
            "Source conflict: repository changed since snapshot; re-run against current sources"
        )
    # Preflight the entire patch before modifying any source file.
    for relative in changes:
        destination = safe_path(source, relative)
        current = digest(destination) if destination.is_file() else None
        if current != before.get(relative) or (destination.exists() and not destination.is_file()):
            raise ValueError(f"Source conflict: {relative}; no changes applied")
        safe_path(workspace, relative)
    backup.mkdir(parents=True, exist_ok=False)
    applied = []
    try:
        for relative in changes:
            destination = safe_path(source, relative)
            updated = safe_path(workspace, relative)
            if destination.exists():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(destination, saved)
            applied.append(relative)
            if updated.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(updated, destination)
            else:
                destination.unlink()
    except BaseException:
        for relative in reversed(applied):
            saved, destination = backup / relative, source / relative
            if saved.exists():
                shutil.copyfile(saved, destination)
            elif destination.exists():
                destination.unlink()
        raise
    return changes
