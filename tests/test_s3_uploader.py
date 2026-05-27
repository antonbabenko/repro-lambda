from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from repro_lambda.s3_uploader import S3Uploader, UploadResult


@pytest.fixture
def bucket():
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(
            Bucket="dev-ctf-lambda-artifacts",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
        )
        yield "dev-ctf-lambda-artifacts"


def _make_zip(tmp_path: Path, content: bytes = b"PK\x05\x06" + b"\x00" * 18) -> Path:
    zip_path = tmp_path / "lambda.zip"
    zip_path.write_bytes(content)
    return zip_path


def test_head_returns_false_when_object_missing(bucket: str):
    uploader = S3Uploader(region="eu-west-1")
    assert uploader.exists(bucket=bucket, key="lambdas/app/aaa.zip") is False


def test_upload_succeeds_on_first_put(bucket: str, tmp_path: Path):
    uploader = S3Uploader(region="eu-west-1")
    zip_path = _make_zip(tmp_path)
    result = uploader.upload(bucket=bucket, key="lambdas/app/aaa.zip", body_path=zip_path)
    assert result == UploadResult.UPLOADED
    assert uploader.exists(bucket=bucket, key="lambdas/app/aaa.zip") is True


def test_upload_treats_412_precondition_failed_as_already_present(bucket: str, tmp_path: Path):
    uploader = S3Uploader(region="eu-west-1")
    zip_path = _make_zip(tmp_path)
    uploader.upload(bucket=bucket, key="lambdas/app/aaa.zip", body_path=zip_path)
    result = uploader.upload(bucket=bucket, key="lambdas/app/aaa.zip", body_path=zip_path)
    assert result == UploadResult.ALREADY_PRESENT


def test_upload_passes_if_none_match_header(bucket: str, tmp_path: Path, mocker):
    uploader = S3Uploader(region="eu-west-1")
    spy = mocker.spy(uploader._client, "put_object")
    zip_path = _make_zip(tmp_path)
    uploader.upload(bucket=bucket, key="lambdas/app/aaa.zip", body_path=zip_path)

    call_kwargs = spy.call_args.kwargs
    assert call_kwargs.get("IfNoneMatch") == "*"
