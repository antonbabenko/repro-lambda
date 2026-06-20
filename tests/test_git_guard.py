import subprocess
from pathlib import Path

import pytest

from repro_lambda.git_guard import DirtyWorktreeError, ensure_clean_worktree


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    src = tmp_path / "handler"
    src.mkdir()
    (src / "app.py").write_text("ok\n")
    subprocess.run(["git", "add", "handler/app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_ensure_clean_worktree_passes_on_clean(clean_repo: Path):
    ensure_clean_worktree(repo_root=clean_repo, source_dir="handler")


def test_ensure_clean_worktree_raises_on_modified_tracked_file(clean_repo: Path):
    (clean_repo / "handler" / "app.py").write_text("changed\n")
    with pytest.raises(DirtyWorktreeError, match="handler/app.py"):
        ensure_clean_worktree(repo_root=clean_repo, source_dir="handler")


def test_ensure_clean_worktree_ignores_untracked_files(clean_repo: Path):
    (clean_repo / "handler" / "scratch.py").write_text("untracked\n")
    ensure_clean_worktree(repo_root=clean_repo, source_dir="handler")


def test_ensure_clean_worktree_allow_dirty_skips_check(clean_repo: Path):
    (clean_repo / "handler" / "app.py").write_text("changed\n")
    ensure_clean_worktree(repo_root=clean_repo, source_dir="handler", allow_dirty=True)
