import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from repro_lambda.build import BuildResult, build_one
from repro_lambda.catalog import Catalog
from repro_lambda.manifest import BuilderConfig, LambdaSpec


@pytest.fixture
def git_repo_with_nodejs_sample(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    h = tmp_path / "handler"; h.mkdir()
    (h / "index.js").write_text("exports.handler = async () => ({});\n")
    (h / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    (h / "package-lock.json").write_text(
        '{"name": "x", "version": "1.0.0", "lockfileVersion": 3, "requires": true, "packages": {}}'
    )
    subprocess.run(["git", "add", "handler/"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _nodejs_spec() -> LambdaSpec:
    return LambdaSpec(
        logical_name="edge", source_dir="handler",
        requirements_lock="handler/package-lock.json",
        package_json="handler/package.json",
        runtime="nodejs22.x", arch="x86_64", handler="index.handler",
        region="us-east-1", package_manager="npm", lambda_at_edge=True,
    )


def _nodejs_builder() -> BuilderConfig:
    return BuilderConfig(
        base_image_python="public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64,
        base_image_nodejs="public.ecr.aws/lambda/nodejs:22@sha256:" + "0" * 64,
        include_patterns=["**/*.js", "**/*.json"],
        exclude_patterns=[".git/**", "node_modules/**"],
    )


def test_build_one_nodejs_routes_to_build_nodejs_lambda(
    git_repo_with_nodejs_sample: Path, mocker
):
    mock_python = mocker.patch("repro_lambda.build.build_python_lambda")
    mock_nodejs = mocker.patch(
        "repro_lambda.build.build_nodejs_lambda",
        side_effect=lambda **kw: kw["out_zip"].write_bytes(b"PK\x05\x06" + b"\x00" * 18),
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="dev-ctf-lambda-artifacts-us-east-1")
        outcome = build_one(
            repo_root=git_repo_with_nodejs_sample,
            spec=_nodejs_spec(), builder=_nodejs_builder(),
            bucket="dev-ctf-lambda-artifacts",
            catalog=Catalog(lambdas={}),
            source_commit="deadbeef",
        )

    assert outcome.outcome == BuildResult.BUILT_AND_UPLOADED
    mock_python.assert_not_called()
    mock_nodejs.assert_called_once()
    # NOTE: plan snippet had 'base_image' typo; build_nodejs_lambda's kwarg is base_image_nodejs.
    assert "nodejs:22" in mock_nodejs.call_args.kwargs["base_image_nodejs"]


def test_build_one_lambda_at_edge_uses_us_east_1_bucket(
    git_repo_with_nodejs_sample: Path, mocker
):
    mocker.patch(
        "repro_lambda.build.build_nodejs_lambda",
        side_effect=lambda **kw: kw["out_zip"].write_bytes(b"PK\x05\x06" + b"\x00" * 18),
    )
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="dev-ctf-lambda-artifacts-us-east-1")
        outcome = build_one(
            repo_root=git_repo_with_nodejs_sample,
            spec=_nodejs_spec(), builder=_nodejs_builder(),
            bucket="dev-ctf-lambda-artifacts",
            catalog=Catalog(lambdas={}),
            source_commit="deadbeef",
        )
        s3.head_object(
            Bucket="dev-ctf-lambda-artifacts-us-east-1",
            Key=f"lambdas/edge/{outcome.sha256}.zip",
        )
