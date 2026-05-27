import subprocess
from pathlib import Path

import pytest

from repro_lambda.manifest import BuilderConfig
from repro_lambda.source_stager import stage_source


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Initialize a tiny git repo with tracked + untracked files."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)

    src = tmp_path / "handler"
    src.mkdir()
    (src / "app.py").write_text("def lambda_handler(e, c): return 'ok'\n")
    (src / "data.json").write_text('{"k": "v"}\n')
    (src / "README.md").write_text("# notes\n")

    subprocess.run(
        ["git", "add", "handler/app.py", "handler/data.json", "handler/README.md"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    # Untracked junk that must NOT appear in staged tree
    (src / "scratch.py").write_text("untracked\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "app.cpython-313.pyc").write_text("byte")

    return tmp_path


def test_stage_source_includes_only_git_tracked_matching_patterns(git_repo: Path, tmp_path: Path):
    builder = BuilderConfig(
        base_image_python="x@sha256:0",
        include_patterns=["**/*.py", "**/*.json"],
        exclude_patterns=["__pycache__/**", "*.pyc"],
    )
    stage_dir = tmp_path / "stage"
    file_list = stage_source(
        repo_root=git_repo, source_dir="handler", builder=builder, stage_dir=stage_dir
    )

    assert sorted(file_list) == ["handler/app.py", "handler/data.json"]

    assert (
        stage_dir / "source" / "app.py"
    ).read_text() == "def lambda_handler(e, c): return 'ok'\n"
    assert (stage_dir / "source" / "data.json").read_text() == '{"k": "v"}\n'

    assert not (stage_dir / "source" / "README.md").exists()
    assert not (stage_dir / "source" / "scratch.py").exists()


def test_stage_source_respects_exclude_patterns(git_repo: Path, tmp_path: Path):
    src = git_repo / "handler"
    (src / "compiled.pyc").write_bytes(b"\x03\xf3\r\n")
    subprocess.run(["git", "add", "-f", "handler/compiled.pyc"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add pyc"], cwd=git_repo, check=True)

    builder = BuilderConfig(
        base_image_python="x@sha256:0",
        include_patterns=["**/*"],
        exclude_patterns=["*.pyc"],
    )
    stage_dir = tmp_path / "stage"
    file_list = stage_source(git_repo, "handler", builder, stage_dir)
    assert "handler/compiled.pyc" not in file_list


def test_stage_source_preserves_executable_bit(git_repo: Path, tmp_path: Path):
    src = git_repo / "handler"
    script = src / "tool.py"
    script.write_text("#!/usr/bin/env python\nprint('hi')\n")
    script.chmod(0o755)
    subprocess.run(["git", "add", "handler/tool.py"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "update-index", "--chmod=+x", "handler/tool.py"], cwd=git_repo, check=True
    )
    subprocess.run(["git", "commit", "-q", "-m", "add exec"], cwd=git_repo, check=True)

    builder = BuilderConfig(
        base_image_python="x@sha256:0", include_patterns=["**/*"], exclude_patterns=[]
    )
    stage_dir = tmp_path / "stage"
    stage_source(git_repo, "handler", builder, stage_dir)

    staged = stage_dir / "source" / "tool.py"
    assert staged.exists()
    assert staged.stat().st_mode & 0o111, "executable bit not preserved"
