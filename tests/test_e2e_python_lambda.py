"""End-to-end: real docker build of a trivial Python lambda, twice, sha-compare."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from repro_lambda.docker_runner import build_python_lambda
from repro_lambda.manifest import BuilderConfig
from repro_lambda.source_stager import stage_source


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.docker
def test_two_independent_builds_produce_byte_identical_zips(tmp_path: Path):
    if shutil.which("docker") is None:
        pytest.skip("docker not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    h = repo / "handler"
    h.mkdir()
    (h / "app.py").write_text(
        "def lambda_handler(event, context):\n"
        "    return {'statusCode': 200, 'body': 'ok'}\n"
    )
    (h / "requirements.arm64.lock").write_text("")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    builder = BuilderConfig(
        base_image_python="public.ecr.aws/lambda/python:3.13",
        include_patterns=["**/*.py"],
        exclude_patterns=["__pycache__/**", "*.pyc"],
    )

    shas: list[str] = []
    for run_index in range(2):
        stage = tmp_path / f"stage{run_index}"
        stage_source(repo, "handler", builder, stage)
        (stage / "requirements.lock").write_bytes(
            (h / "requirements.arm64.lock").read_bytes()
        )
        out = stage / "lambda.zip"
        build_python_lambda(
            stage_dir=stage,
            out_zip=out,
            base_image=builder.base_image_python,
            arch="arm64",
            python_version="3.13",
        )
        shas.append(_sha256(out))

    assert shas[0] == shas[1], f"non-reproducible: {shas[0]} vs {shas[1]}"
