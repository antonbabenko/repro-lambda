"""Stage git-tracked source files into a tempdir before container build."""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path

from repro_lambda.manifest import BuilderConfig, ExtraFile


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


def _stage_payload_files(
    repo_root: Path, target_root: Path, payload_files: list[ExtraFile]
) -> None:
    """Stage prebuilt files/dirs (CI-materialized, not git-tracked) into the package.

    Each lands at target_root/<dest> (the staged source tree, so it ships in the zip
    and folds into the content hash). Files get the +x bit when `executable`; dirs
    are copied recursively with source perms preserved.
    """
    for ef in payload_files:
        src = repo_root / ef.src
        dest = target_root / ef.dest
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            if ef.executable:
                dest.chmod(dest.stat().st_mode | 0o111)
        else:
            raise FileNotFoundError(f"extra_files src not found: {src} (declared src={ef.src!r})")


def stage_source(
    repo_root: Path,
    source_dir: str,
    builder: BuilderConfig,
    stage_dir: Path,
    *,
    extra_files: list[tuple[Path, str]] | None = None,
    payload_files: list[ExtraFile] | None = None,
) -> list[str]:
    """
    Copy git-tracked files under source_dir into stage_dir/source/, preserving perms.

    `extra_files` are build inputs (e.g. the requirements lock) copied to
    stage_dir/<rel_name> - consumed by the container, not shipped in the zip.

    `payload_files` are prebuilt artifacts copied into stage_dir/source/<dest> so
    they ship in the zip and fold into the content hash.

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

    _stage_payload_files(repo_root, target_root, payload_files or [])

    for src_path, rel_name in extra_files or []:
        if not src_path.is_file():
            raise FileNotFoundError(f"extra_files source not found: {src_path}")
        dest = stage_dir / rel_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src_path.read_bytes())

    return filtered
