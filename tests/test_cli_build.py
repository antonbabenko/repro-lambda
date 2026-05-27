import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
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
    (repo / "handler" / "app.py").write_text(
        "def lambda_handler(e, c): return {'statusCode': 200}\n"
    )
    (repo / "handler" / "requirements.in").write_text("")
    (repo / "handler" / "requirements.arm64.lock").write_text("")
    (repo / "lambdas.toml").write_text(
        '[[lambda]]\n'
        'logical_name      = "app"\n'
        'source_dir        = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime           = "python3.13"\n'
        'arch              = "arm64"\n'
        'handler           = "app.lambda_handler"\n'
        'region            = "eu-west-1"\n'
        'package_manager   = "pip"\n'
        'lambda_at_edge    = false\n'
        'hash_extra        = ""\n'
        '\n'
        '[builder]\n'
        'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:' + "0" * 64 + '"\n'
        'include_patterns  = ["**/*.py"]\n'
        'exclude_patterns  = ["__pycache__/**", "*.pyc"]\n'
    )

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_cli_build_dry_run_succeeds_without_docker_or_s3(consumer_repo: Path, mocker):
    mock_docker = mocker.patch("repro_lambda.build.build_python_lambda")
    mock_uploader = mocker.patch("repro_lambda.build.S3Uploader")

    result = runner.invoke(
        app,
        ["build", "app", "--manifest", str(consumer_repo / "lambdas.toml"), "--dry-run"],
    )
    assert result.exit_code == 0, result.stdout
    assert "dry_run" in result.stdout
    mock_docker.assert_not_called()
    mock_uploader.assert_not_called()


def test_cli_build_emits_catalog_on_success(consumer_repo: Path, mocker):
    mocker.patch(
        "repro_lambda.build.build_python_lambda",
        side_effect=lambda **kw: kw["out_zip"].write_bytes(b"PK" + b"\x00" * 100),
    )
    with mock_aws():
        boto3.client("s3", region_name="eu-west-1").create_bucket(
            Bucket="dev-ctf-lambda-artifacts",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
        )
        result = runner.invoke(
            app,
            ["build", "app", "--manifest", str(consumer_repo / "lambdas.toml")],
            env={"REPRO_LAMBDA_BUCKET": "dev-ctf-lambda-artifacts"},
        )
    assert result.exit_code == 0, result.stdout
    catalog_path = consumer_repo / "builds" / "catalog.json"
    assert catalog_path.exists()
    assert "current" in catalog_path.read_text()


def test_cli_build_refuses_dirty_worktree(consumer_repo: Path):
    (consumer_repo / "handler" / "app.py").write_text("changed\n")
    result = runner.invoke(
        app,
        ["build", "app", "--manifest", str(consumer_repo / "lambdas.toml"), "--dry-run"],
    )
    assert result.exit_code != 0


def test_cli_build_allow_dirty_bypasses_guard(consumer_repo: Path, mocker):
    (consumer_repo / "handler" / "app.py").write_text("changed\n")
    mocker.patch("repro_lambda.build.build_python_lambda")
    mocker.patch("repro_lambda.build.S3Uploader")
    result = runner.invoke(
        app,
        [
            "build",
            "app",
            "--manifest",
            str(consumer_repo / "lambdas.toml"),
            "--dry-run",
            "--allow-dirty",
        ],
    )
    assert result.exit_code == 0, result.stdout
