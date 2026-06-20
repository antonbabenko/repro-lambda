"""Refuse to build with uncommitted tracked changes (unless --allow-dirty)."""

from __future__ import annotations

import subprocess
from pathlib import Path


class DirtyWorktreeError(RuntimeError):
    """Raised when source_dir has uncommitted tracked changes."""


def ensure_clean_worktree(
    *,
    repo_root: Path,
    source_dir: str,
    allow_dirty: bool = False,
) -> None:
    if allow_dirty:
        return
    result = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            source_dir,
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise DirtyWorktreeError(
            f"Uncommitted changes in {source_dir}:\n{result.stdout}"
            "Commit or pass --allow-dirty for local iteration."
        )
