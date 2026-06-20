import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from repro_lambda.build import BuildResult, build_one, compute_sha_for
from repro_lambda.catalog import Catalog
from repro_lambda.manifest import BuilderConfig, LambdaSpec


@pytest.fixture
def git_repo_with_sample(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    h = tmp_path / "handler"
    h.mkdir()
    (h / "app.py").write_text(
        "def lambda_handler(event, context):\n    return {'statusCode': 200, 'body': 'ok'}\n"
    )
    (h / "requirements.in").write_text("")
    (h / "requirements.arm64.lock").write_text("")
    subprocess.run(["git", "add", "handler/"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _make_spec() -> LambdaSpec:
    return LambdaSpec(
        logical_name="app",
        source_dir="handler",
        requirements_lock="handler/requirements.${arch}.lock",
        runtime="python3.13",
        arch="arm64",
        handler="app.lambda_handler",
        region="eu-west-1",
        package_manager="pip",
    )


def _make_builder() -> BuilderConfig:
    return BuilderConfig(
        base_image_python="public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64,
        include_patterns=["**/*.py"],
        exclude_patterns=["__pycache__/**", "*.pyc"],
    )


def test_build_one_cache_hit_skips_docker_and_returns_existing_sha(
    git_repo_with_sample: Path, mocker
):
    spec = _make_spec()
    builder = _make_builder()
    catalog = Catalog(lambdas={})

    mock_docker = mocker.patch("repro_lambda.build.build_python_lambda")

    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(
            Bucket="dev-test-lambda-artifacts",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
        )
        sha = compute_sha_for(repo_root=git_repo_with_sample, spec=spec, builder=builder)
        s3.put_object(
            Bucket="dev-test-lambda-artifacts",
            Key=f"lambdas/app/{sha}.zip",
            Body=b"existing",
        )

        outcome = build_one(
            repo_root=git_repo_with_sample,
            spec=spec,
            builder=builder,
            bucket="dev-test-lambda-artifacts",
            catalog=catalog,
            source_commit="deadbeef",
        )

    assert outcome.outcome == BuildResult.CACHE_HIT
    assert outcome.sha256 == sha
    mock_docker.assert_not_called()
    assert catalog.lambdas["app"].current == sha


def test_build_one_cache_miss_runs_docker_uploads_and_records(git_repo_with_sample: Path, mocker):
    spec = _make_spec()
    builder = _make_builder()
    catalog = Catalog(lambdas={})

    def fake_build(*, stage_dir, out_zip, **_):
        out_zip.write_bytes(b"PK" + b"\x00" * 100)

    mocker.patch("repro_lambda.build.build_python_lambda", side_effect=fake_build)

    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(
            Bucket="dev-test-lambda-artifacts",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
        )

        outcome = build_one(
            repo_root=git_repo_with_sample,
            spec=spec,
            builder=builder,
            bucket="dev-test-lambda-artifacts",
            catalog=catalog,
            source_commit="deadbeef",
        )

        assert outcome.outcome == BuildResult.BUILT_AND_UPLOADED
        s3.head_object(
            Bucket="dev-test-lambda-artifacts",
            Key=f"lambdas/app/{outcome.sha256}.zip",
        )

    assert catalog.lambdas["app"].current == outcome.sha256
    assert catalog.lambdas["app"].history[0].source_commit == "deadbeef"


def test_build_one_dry_run_computes_hash_but_skips_upload(git_repo_with_sample: Path, mocker):
    spec = _make_spec()
    builder = _make_builder()
    catalog = Catalog(lambdas={})

    mock_docker = mocker.patch("repro_lambda.build.build_python_lambda")
    mock_uploader = mocker.patch("repro_lambda.build.S3Uploader")

    outcome = build_one(
        repo_root=git_repo_with_sample,
        spec=spec,
        builder=builder,
        bucket="dev-test-lambda-artifacts",
        catalog=catalog,
        source_commit="deadbeef",
        dry_run=True,
    )

    assert outcome.outcome == BuildResult.DRY_RUN
    assert outcome.sha256
    mock_docker.assert_not_called()
    mock_uploader.assert_not_called()
    assert "app" not in catalog.lambdas
