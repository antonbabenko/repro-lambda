"""Promote (dev -> prod copy by content hash) unit + CLI coverage."""

from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from repro_lambda.cli import app
from repro_lambda.manifest import LambdaSpec
from repro_lambda.promote import (
    MissingSourceArtifactError,
    PromoteResult,
    UnknownShaError,
    promote_one,
    sha_from_catalog,
)
from repro_lambda.s3_uploader import S3Uploader, UploadResult

runner = CliRunner()

DEV = "dev-test-lambda-artifacts"
PROD = "prod-test-lambda-artifacts"


def _make_bucket(client, name: str, region: str) -> None:
    if region == "us-east-1":
        client.create_bucket(Bucket=name)
    else:
        client.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})


def _spec(name: str, *, region: str = "eu-west-1", at_edge: bool = False) -> LambdaSpec:
    return LambdaSpec(
        logical_name=name,
        source_dir="handler",
        requirements_lock="handler/requirements.${arch}.lock",
        runtime="python3.13",
        arch="x86_64" if at_edge else "arm64",
        handler="app.lambda_handler",
        region=region,
        lambda_at_edge=at_edge,
    )


# --- S3Uploader.copy -------------------------------------------------------


def test_copy_uploads_then_reports_already_present():
    with mock_aws():
        c = boto3.client("s3", region_name="eu-west-1")
        _make_bucket(c, DEV, "eu-west-1")
        _make_bucket(c, PROD, "eu-west-1")
        c.put_object(Bucket=DEV, Key="lambdas/app/abc.zip", Body=b"PK\x05\x06" + b"\x00" * 18)

        up = S3Uploader(region="eu-west-1")
        first = up.copy(src_bucket=DEV, dst_bucket=PROD, key="lambdas/app/abc.zip")
        second = up.copy(src_bucket=DEV, dst_bucket=PROD, key="lambdas/app/abc.zip")

        assert first == UploadResult.UPLOADED
        assert second == UploadResult.ALREADY_PRESENT
        assert up.exists(bucket=PROD, key="lambdas/app/abc.zip")


# --- promote_one -----------------------------------------------------------


def test_promote_one_copies_regional_lambda():
    with mock_aws():
        c = boto3.client("s3", region_name="eu-west-1")
        _make_bucket(c, DEV, "eu-west-1")
        _make_bucket(c, PROD, "eu-west-1")
        c.put_object(Bucket=DEV, Key="lambdas/app/sha1.zip", Body=b"z")

        out = promote_one(spec=_spec("app"), sha="sha1", dev_bucket=DEV, prod_bucket=PROD)

        assert out.outcome == PromoteResult.PROMOTED
        assert out.src_bucket == DEV
        assert out.dst_bucket == PROD
        assert out.bucket_key == "lambdas/app/sha1.zip"
        assert c.head_object(Bucket=PROD, Key="lambdas/app/sha1.zip")


def test_promote_one_edge_lambda_uses_us_east_1_bucket_variant():
    with mock_aws():
        eu = boto3.client("s3", region_name="eu-west-1")
        us = boto3.client("s3", region_name="us-east-1")
        _make_bucket(eu, DEV, "eu-west-1")
        _make_bucket(eu, PROD, "eu-west-1")
        _make_bucket(us, f"{DEV}-us-east-1", "us-east-1")
        _make_bucket(us, f"{PROD}-us-east-1", "us-east-1")
        us.put_object(Bucket=f"{DEV}-us-east-1", Key="lambdas/edge/e1.zip", Body=b"z")

        out = promote_one(
            spec=_spec("edge", region="us-east-1", at_edge=True),
            sha="e1",
            dev_bucket=DEV,
            prod_bucket=PROD,
        )

        assert out.src_bucket == f"{DEV}-us-east-1"
        assert out.dst_bucket == f"{PROD}-us-east-1"
        assert us.head_object(Bucket=f"{PROD}-us-east-1", Key="lambdas/edge/e1.zip")


def test_promote_one_missing_source_raises():
    with mock_aws():
        c = boto3.client("s3", region_name="eu-west-1")
        _make_bucket(c, DEV, "eu-west-1")
        _make_bucket(c, PROD, "eu-west-1")

        with pytest.raises(MissingSourceArtifactError):
            promote_one(spec=_spec("app"), sha="missing", dev_bucket=DEV, prod_bucket=PROD)


def test_promote_one_dry_run_touches_no_s3():
    out = promote_one(spec=_spec("app"), sha="sha1", dev_bucket=DEV, prod_bucket=PROD, dry_run=True)
    assert out.outcome == PromoteResult.DRY_RUN
    assert out.bucket_key == "lambdas/app/sha1.zip"


# --- sha_from_catalog ------------------------------------------------------


def test_sha_from_catalog_reads_current(tmp_path: Path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        '{"schema_version": 1, "lambdas": {"app": {"current": "deadbeef", "history": []}}}\n'
    )
    assert sha_from_catalog(catalog, "app") == "deadbeef"


def test_sha_from_catalog_unknown_lambda_raises(tmp_path: Path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema_version": 1, "lambdas": {}}\n')
    with pytest.raises(UnknownShaError):
        sha_from_catalog(catalog, "app")


# --- CLI -------------------------------------------------------------------


def _consumer(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    (repo / "handler").mkdir(parents=True)
    (repo / "lambdas.toml").write_text(
        "[[lambda]]\n"
        'logical_name      = "app"\n'
        'source_dir        = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime           = "python3.13"\n'
        'arch              = "arm64"\n'
        'handler           = "app.lambda_handler"\n'
        'region            = "eu-west-1"\n'
        "\n"
        "[builder]\n"
        'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:' + "0" * 64 + '"\n'
    )
    (repo / "builds").mkdir()
    (repo / "builds" / "catalog.json").write_text(
        '{"schema_version": 1, "lambdas": {"app": {"current": "cafef00d", "history": []}}}\n'
    )
    return repo


def test_cli_promote_copies_from_catalog(tmp_path: Path):
    repo = _consumer(tmp_path)
    with mock_aws():
        c = boto3.client("s3", region_name="eu-west-1")
        _make_bucket(c, DEV, "eu-west-1")
        _make_bucket(c, PROD, "eu-west-1")
        c.put_object(Bucket=DEV, Key="lambdas/app/cafef00d.zip", Body=b"z")

        result = runner.invoke(
            app,
            [
                "promote",
                "app",
                "--manifest",
                str(repo / "lambdas.toml"),
                "--dev-bucket",
                DEV,
                "--prod-bucket",
                PROD,
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "promoted" in result.stdout
        assert c.head_object(Bucket=PROD, Key="lambdas/app/cafef00d.zip")


def test_cli_promote_dry_run_touches_no_s3(tmp_path: Path):
    repo = _consumer(tmp_path)
    result = runner.invoke(
        app,
        [
            "promote",
            "app",
            "--manifest",
            str(repo / "lambdas.toml"),
            "--dev-bucket",
            DEV,
            "--prod-bucket",
            PROD,
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "dry_run" in result.stdout


def test_cli_promote_unknown_target_exits_2(tmp_path: Path):
    repo = _consumer(tmp_path)
    result = runner.invoke(
        app,
        [
            "promote",
            "nope",
            "--manifest",
            str(repo / "lambdas.toml"),
            "--dev-bucket",
            DEV,
            "--prod-bucket",
            PROD,
        ],
    )
    assert result.exit_code == 2


_EXPLICIT_SHA = "a" * 64  # 64-hex, deliberately NOT the catalog's "cafef00d"


def test_cli_promote_explicit_sha_bypasses_catalog(tmp_path: Path):
    repo = _consumer(tmp_path)
    with mock_aws():
        c = boto3.client("s3", region_name="eu-west-1")
        _make_bucket(c, DEV, "eu-west-1")
        _make_bucket(c, PROD, "eu-west-1")
        # Only the explicit-sha object exists; the catalog's current sha does not.
        c.put_object(Bucket=DEV, Key=f"lambdas/app/{_EXPLICIT_SHA}.zip", Body=b"z")

        result = runner.invoke(
            app,
            [
                "promote",
                "app",
                "--manifest",
                str(repo / "lambdas.toml"),
                "--dev-bucket",
                DEV,
                "--prod-bucket",
                PROD,
                "--sha",
                _EXPLICIT_SHA,
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert c.head_object(Bucket=PROD, Key=f"lambdas/app/{_EXPLICIT_SHA}.zip")


def test_cli_promote_sha_rejects_all_target(tmp_path: Path):
    repo = _consumer(tmp_path)
    result = runner.invoke(
        app,
        [
            "promote",
            "all",
            "--manifest",
            str(repo / "lambdas.toml"),
            "--dev-bucket",
            DEV,
            "--prod-bucket",
            PROD,
            "--sha",
            _EXPLICIT_SHA,
        ],
    )
    assert result.exit_code == 2  # --sha forbidden with 'all'


def test_cli_promote_sha_invalid_hex_exits_2(tmp_path: Path):
    repo = _consumer(tmp_path)
    result = runner.invoke(
        app,
        [
            "promote",
            "app",
            "--manifest",
            str(repo / "lambdas.toml"),
            "--dev-bucket",
            DEV,
            "--prod-bucket",
            PROD,
            "--sha",
            "not-a-valid-sha",
        ],
    )
    assert result.exit_code == 2  # not 64-hex
