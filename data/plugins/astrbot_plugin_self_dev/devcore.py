"""File-level helpers for the self-dev plugin.

Everything here is deliberately dumb: copy, diff, compile, restore.
No network, no LLM, no state beyond the paths handed in.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = (
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    "jobs",
    "temp",
)

SKIP_GLOBS = ("*.db", "*.db-wal", "*.db-shm", "*.pyc", "*.log", "*.zip", "*.tar.gz", "*.png", "*.jpg", "*.gif", "*.mp4")

TEXT_SUFFIXES = {
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".toml",
    ".cfg",
    ".ini",
    ".html",
    ".css",
    ".js",
}

MAX_FILE_BYTES = 400_000


def is_editable(rel: str) -> bool:
    """Return True when a relative path may be read or written by the coder."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return False
    return p.suffix.lower() in TEXT_SUFFIXES


def snapshot(src: Path, dst: Path) -> None:
    """Copy a plugin directory into a scratch workspace."""
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP_DIRS, *SKIP_GLOBS))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def list_tree(root: Path, max_entries: int = 200) -> str:
    """Render a shallow tree of the workspace."""
    lines: list[str] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.is_file() and p.suffix in {".db", ".pyc"}:
            continue
        depth = len(rel.parts) - 1
        if depth > 3:
            continue
        lines.append(("  " * depth) + rel.name + ("/" if p.is_dir() else f"  ({p.stat().st_size}B)"))
        if len(lines) >= max_entries:
            lines.append("... (truncated)")
            break
    return "\n".join(lines) or "(empty)"


def _visible(rel: str) -> bool:
    """True when a relative path should show up in a diff."""
    if any(part in SKIP_DIRS for part in Path(rel).parts):
        return False
    return not rel.endswith((".pyc", ".db", ".db-wal", ".db-shm", ".log"))


def diff_trees(
    base: Path, work: Path, max_chars: int = 12000
) -> tuple[str, list[str], list[str], list[str]]:
    """Return (diff_text, changed, added, removed) between the base and the workspace."""
    base_files = {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and _visible(p.relative_to(base).as_posix())}
    work_files = {p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file() and _visible(p.relative_to(work).as_posix())}

    changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    chunks: list[str] = []

    for rel in sorted(work_files):
        if rel not in base_files:
            added.append(rel)
            chunks.append(f"+++ {rel} (new file)")
            continue
        if not is_editable(rel):
            continue
        a, b = base / rel, work / rel
        try:
            if a.stat().st_size > MAX_FILE_BYTES or b.stat().st_size > MAX_FILE_BYTES:
                continue
            ta, tb = read_text(a), read_text(b)
        except OSError:
            continue
        if ta == tb:
            continue
        changed.append(rel)
        diff = difflib.unified_diff(
            ta.splitlines(),
            tb.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
            n=2,
        )
        chunks.append("\n".join(diff))

    for rel in sorted(base_files - work_files):
        removed.append(rel)
        chunks.append(f"--- {rel} (deleted)")

    text = "\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (diff 截断，原始长度 {len(text)} 字符)"
    return text, changed, added, removed


def compile_tree(root: Path, timeout: int = 120) -> tuple[bool, str]:
    """Byte-compile every .py in the tree."""
    files = [str(p) for p in root.rglob("*.py")]
    if not files:
        return True, "no python files"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", *files],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "py_compile timed out"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-2000:] if output else "ok"


def backup_files(plugin_dir: Path, backup_dir: Path, rels: list[str]) -> None:
    for rel in rels:
        src = plugin_dir / rel
        if not src.exists():
            continue
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def copy_into(work: Path, plugin_dir: Path, rels: list[str]) -> None:
    for rel in rels:
        dst = plugin_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / rel, dst)


def restore_files(plugin_dir: Path, backup_dir: Path, rels: list[str], added: list[str]) -> None:
    for rel in rels:
        bak = backup_dir / rel
        if bak.exists():
            shutil.copy2(bak, plugin_dir / rel)
    for rel in added:
        target = plugin_dir / rel
        if target.exists():
            target.unlink()
