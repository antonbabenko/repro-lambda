"""Stage git-tracked source files into a tempdir before container build."""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path

from repro_lambda.manifest import BuilderConfig


def _git_ls_files(repo_root: Path, source_dir: str) -> list[str]:
    """Return paths of all tracked files under source_dir, relative to repo_root."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "-z", "--", source_dir],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    raw = result.stdout.decode("utf-8")
    return [p for p in raw.split("\x00") if p]


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def _filter_paths(paths: list[str], include: list[str], exclude: list[str]) -> list[str]:
    kept: list[str] = []
    for p in paths:
        if include and not _matches_any(p, include):
            continue
        if exclude and _matches_any(p, exclude):
            continue
        kept.append(p)
    return kept


def stage_source(
    repo_root: Path,
    source_dir: str,
    builder: BuilderConfig,
    stage_dir: Path,
) -> list[str]:
    """
    Copy git-tracked files under source_dir into stage_dir/source/, preserving perms.

    Returns the sorted list of relative paths (from repo_root) that were staged.
    """
    tracked = _git_ls_files(repo_root, source_dir)
    filtered = _filter_paths(tracked, builder.include_patterns, builder.exclude_patterns)
    filtered.sort()

    target_root = stage_dir / "source"
    target_root.mkdir(parents=True, exist_ok=True)

    for rel in filtered:
        src = repo_root / rel
        rel_within_source = Path(rel).relative_to(source_dir)
        dst = target_root / rel_within_source
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        src_mode = src.stat().st_mode
        if src_mode & 0o111:
            dst.chmod(dst.stat().st_mode | 0o111)

    return filtered
