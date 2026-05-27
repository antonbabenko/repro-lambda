import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repro_lambda.cli import app

runner = CliRunner()


@pytest.fixture
def consumer_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    (repo / "handler").mkdir()
    (repo / "handler" / "app.py").write_text("def lambda_handler(e, c): return 'ok'\n")
    (repo / "handler" / "requirements.in").write_text("")
    (repo / "handler" / "requirements.arm64.lock").write_text("")
    (repo / "lambdas.toml").write_text(
        '[[lambda]]\n'
        'logical_name = "app"\n'
        'source_dir = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime = "python3.13"\n'
        'arch = "arm64"\n'
        'handler = "app.lambda_handler"\n'
        '[builder]\n'
        'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:' + "0" * 64 + '"\n'
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_verify_passes_when_two_builds_produce_identical_zips(consumer_repo: Path, mocker):
    def fake_build(*, stage_dir, out_zip, **_):
        out_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    mocker.patch("repro_lambda.build.build_python_lambda", side_effect=fake_build)
    mocker.patch("repro_lambda.verify.build_python_lambda", side_effect=fake_build)

    result = runner.invoke(
        app,
        [
            "build",
            "app",
            "--manifest",
            str(consumer_repo / "lambdas.toml"),
            "--verify",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "reproducible" in result.stdout.lower()


def test_verify_fails_when_two_builds_produce_different_zips(consumer_repo: Path, mocker):
    calls = {"n": 0}

    def fake_build(*, stage_dir, out_zip, **_):
        calls["n"] += 1
        out_zip.write_bytes(b"PK\x05\x06" + bytes([calls["n"] % 256]) * 18)

    mocker.patch("repro_lambda.verify.build_python_lambda", side_effect=fake_build)

    result = runner.invoke(
        app,
        [
            "build",
            "app",
            "--manifest",
            str(consumer_repo / "lambdas.toml"),
            "--verify",
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
