"""End-to-end: real docker build of the sample_nodejs_lambda fixture, twice, sha-compare."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from repro_lambda.docker_runner import build_nodejs_lambda
from repro_lambda.manifest import BuilderConfig
from repro_lambda.source_stager import stage_source


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_FIXTURE = Path(__file__).parent / "fixtures" / "sample_nodejs_lambda"


@pytest.mark.docker
def test_nodejs_two_independent_builds_produce_byte_identical_zips(tmp_path: Path):
    if shutil.which("docker") is None:
        pytest.skip("docker not available")

    repo = tmp_path / "repo"
    shutil.copytree(_FIXTURE, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    # E2E uses unpinned upstream tags (the fixture's lambdas.toml has 0-digest
    # placeholders that are not pullable). The plan's byte-compat reference
    # against a pinned digest is T11, not T10.
    builder = BuilderConfig(
        base_image_python="public.ecr.aws/lambda/python:3.13",
        base_image_nodejs="public.ecr.aws/lambda/nodejs:22",
        include_patterns=["**/*.js", "**/*.json"],
        exclude_patterns=[".git/**", "node_modules/**"],
    )

    extras = [
        (repo / "handler" / "package.json", "package.json"),
        (repo / "handler" / "package-lock.json", "package-lock.json"),
    ]

    shas: list[str] = []
    for run_index in range(2):
        stage = tmp_path / f"stage{run_index}"
        stage_source(
            repo_root=repo,
            source_dir="handler",
            builder=builder,
            stage_dir=stage,
            extra_files=extras,
        )
        out = stage / "lambda.zip"
        build_nodejs_lambda(
            stage_dir=stage,
            out_zip=out,
            base_image_nodejs=builder.base_image_nodejs,
            base_image_python=builder.base_image_python,
            arch="x86_64",
            node_version="22",
        )
        shas.append(_sha256(out))

    assert shas[0] == shas[1], f"non-reproducible: {shas[0]} vs {shas[1]}"
