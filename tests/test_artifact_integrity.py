"""The bytes behind a content-addressed key must be the bytes this source builds.

The key is an INPUT hash anyone with the source can compute, and the bucket is
first-write-wins. So an object that already sits under the key (pre-seeded, raced, or
legacy) is never trusted by its key alone: its zip sha256 is recorded at first write and
checked on every cache hit, upload collision and copy.
"""

import hashlib
import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from repro_lambda.build import BuildResult, build_one, compute_sha_for
from repro_lambda.catalog import Catalog
from repro_lambda.manifest import BuilderConfig, LambdaSpec
from repro_lambda.s3_uploader import (
    SHA256_METADATA_KEY,
    ArtifactIntegrityError,
    S3Uploader,
    UploadResult,
    code_sha256,
)

BUCKET = "dev-test-lambda-artifacts"
PROD = "prod-test-lambda-artifacts"
REAL_ZIP = b"PK" + b"\x00" * 100
MARKER = b"MARKER-NOT-A-REAL-ZIP"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    h = tmp_path / "handler"
    h.mkdir()
    (h / "app.py").write_text("def lambda_handler(event, context):\n    return 'ok'\n")
    (h / "requirements.arm64.lock").write_text("")
    subprocess.run(["git", "add", "handler/"], cwd=tmp_path, check=True)
    return tmp_path


SPEC = LambdaSpec(
    logical_name="app",
    source_dir="handler",
    requirements_lock="handler/requirements.${arch}.lock",
    runtime="python3.13",
    arch="arm64",
    handler="app.lambda_handler",
    region="eu-west-1",
    package_manager="pip",
)
BUILDER = BuilderConfig(
    base_image_python="public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64,
    include_patterns=["**/*.py"],
    exclude_patterns=["__pycache__/**", "*.pyc"],
)


@pytest.fixture
def s3():
    with mock_aws():
        c = boto3.client("s3", region_name="eu-west-1")
        for b in (BUCKET, PROD):
            c.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        yield c


@pytest.fixture
def docker(mocker):
    def fake_build(*, out_zip, **_):
        out_zip.write_bytes(REAL_ZIP)

    return mocker.patch("repro_lambda.build.build_python_lambda", side_effect=fake_build)


def _build(repo: Path):
    return build_one(
        repo_root=repo,
        spec=SPEC,
        builder=BUILDER,
        bucket=BUCKET,
        catalog=Catalog(lambdas={}),
        source_commit="deadbeef",
    )


def _key(repo: Path) -> str:
    return f"lambdas/app/{compute_sha_for(repo_root=repo, spec=SPEC, builder=BUILDER)}.zip"


# --- build_one -------------------------------------------------------------


def test_preseeded_marker_without_recorded_sha_fails_the_build(repo, s3, docker):
    s3.put_object(Bucket=BUCKET, Key=_key(repo), Body=MARKER, IfNoneMatch="*")

    with pytest.raises(ArtifactIntegrityError):
        _build(repo)
    docker.assert_called_once()  # rebuilt to compare, not trusted by key


def test_preseeded_marker_with_wrong_recorded_sha_fails_the_build(repo, s3, docker):
    s3.put_object(
        Bucket=BUCKET,
        Key=_key(repo),
        Body=MARKER,
        IfNoneMatch="*",
        Metadata={SHA256_METADATA_KEY: _sha(REAL_ZIP)},
    )

    with pytest.raises(ArtifactIntegrityError):
        _build(repo)


def test_object_matching_a_rebuild_without_s3_checksum_cannot_be_pinned(repo, s3, docker):
    # Right bytes, but no S3 ChecksumSHA256 (written by another tool): the plan-time and
    # promote checks read that checksum, so the build refuses to report it pin-grade.
    s3.put_object(Bucket=BUCKET, Key=_key(repo), Body=REAL_ZIP)

    with pytest.raises(ArtifactIntegrityError, match="ChecksumSHA256"):
        _build(repo)
    docker.assert_called_once()


def test_object_with_matching_bytes_and_s3_checksum_is_a_reproduced_cache_hit(repo, s3, docker):
    s3.put_object(
        Bucket=BUCKET,
        Key=_key(repo),
        Body=REAL_ZIP,
        ChecksumAlgorithm="SHA256",
        ChecksumSHA256=code_sha256(_sha(REAL_ZIP)),
    )

    outcome = _build(repo)

    assert (outcome.outcome, outcome.zip_source) == (BuildResult.CACHE_HIT, "reproduced")


def test_upload_collision_with_same_bytes_but_no_s3_checksum_cannot_be_pinned(
    repo, s3, docker, mocker
):
    mocker.patch.object(S3Uploader, "exists", return_value=False)
    s3.put_object(Bucket=BUCKET, Key=_key(repo), Body=REAL_ZIP)

    with pytest.raises(ArtifactIntegrityError, match="ChecksumSHA256"):
        _build(repo)


def test_upload_records_sha_and_next_cache_hit_skips_docker(repo, s3, docker):
    first = _build(repo)
    assert first.outcome == BuildResult.BUILT_AND_UPLOADED
    head = s3.head_object(Bucket=BUCKET, Key=first.bucket_key)
    assert head["Metadata"][SHA256_METADATA_KEY] == _sha(REAL_ZIP)

    docker.reset_mock()
    second = _build(repo)

    assert second.outcome == BuildResult.CACHE_HIT
    docker.assert_not_called()


def test_upload_collision_with_different_bytes_fails(repo, s3, docker, mocker):
    key = _key(repo)
    # The object appears between the existence probe and the PutObject (a race).
    mocker.patch.object(S3Uploader, "exists", return_value=False)
    s3.put_object(Bucket=BUCKET, Key=key, Body=MARKER)

    with pytest.raises(ArtifactIntegrityError):
        _build(repo)


# --- S3Uploader.copy -------------------------------------------------------


def _put(s3, bucket: str, body: bytes, recorded: str | None = None, key="lambdas/app/x.zip"):
    meta = {SHA256_METADATA_KEY: recorded} if recorded else {}
    s3.put_object(Bucket=bucket, Key=key, Body=body, Metadata=meta)
    return key


def test_copy_rejects_source_whose_bytes_differ_from_recorded_sha(s3):
    key = _put(s3, BUCKET, MARKER, recorded=_sha(REAL_ZIP))
    up = S3Uploader(region="eu-west-1")

    with pytest.raises(ArtifactIntegrityError):
        up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key)
    assert not up.exists(bucket=PROD, key=key)


def test_copy_rejects_unrecorded_source_without_expected_sha(s3):
    key = _put(s3, BUCKET, REAL_ZIP)
    up = S3Uploader(region="eu-west-1")

    with pytest.raises(ArtifactIntegrityError):
        up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key)
    with pytest.raises(ArtifactIntegrityError):
        up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key, expected_sha256=_sha(MARKER))
    assert not up.exists(bucket=PROD, key=key)

    assert up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key, expected_sha256=_sha(REAL_ZIP)) is (
        UploadResult.UPLOADED
    )
    assert up.verify(bucket=PROD, key=key, expected_sha256=_sha(REAL_ZIP)) == _sha(REAL_ZIP)


def test_self_asserted_record_is_not_trusted_against_a_pin(s3):
    # A writer that plants foreign bytes can also write a matching record. The record
    # alone then passes; only the pinned sha256 (written by the master build) catches it.
    key = _put(s3, BUCKET, MARKER, recorded=_sha(MARKER))
    up = S3Uploader(region="eu-west-1")

    assert up.verify(bucket=BUCKET, key=key) == _sha(MARKER)
    with pytest.raises(ArtifactIntegrityError):
        up.verify(bucket=BUCKET, key=key, expected_sha256=_sha(REAL_ZIP))
    with pytest.raises(ArtifactIntegrityError):
        up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key, expected_sha256=_sha(REAL_ZIP))
    assert not up.exists(bucket=PROD, key=key)


def test_build_reports_the_zip_sha256_it_verified(repo, s3, docker):
    built = _build(repo)
    assert built.zip_sha256 == _sha(REAL_ZIP)
    cached = _build(repo)
    assert cached.outcome == BuildResult.CACHE_HIT
    assert cached.zip_sha256 == _sha(REAL_ZIP)


def test_self_asserted_foreign_cache_hit_reports_the_foreign_sha(repo, s3, docker):
    # A self-consistent planted object is a cache hit, but the build reports ITS bytes'
    # sha256, so the pin binds what is really there and the later pin check (promote,
    # Terraform) never matches a reviewed rebuild of the source.
    s3.put_object(
        Bucket=BUCKET,
        Key=_key(repo),
        Body=MARKER,
        Metadata={SHA256_METADATA_KEY: _sha(MARKER)},
    )
    outcome = _build(repo)
    assert outcome.zip_sha256 == _sha(MARKER) != _sha(REAL_ZIP)


def test_copy_checks_expected_sha(s3):
    key = _put(s3, BUCKET, REAL_ZIP, recorded=_sha(REAL_ZIP))
    up = S3Uploader(region="eu-west-1")

    with pytest.raises(ArtifactIntegrityError):
        up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key, expected_sha256=_sha(MARKER))


def test_copy_rejects_existing_destination_with_other_bytes(s3):
    key = _put(s3, BUCKET, REAL_ZIP, recorded=_sha(REAL_ZIP))
    _put(s3, PROD, MARKER, key=key)
    up = S3Uploader(region="eu-west-1")

    with pytest.raises(ArtifactIntegrityError):
        up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key)


def test_copy_records_sha_on_destination(s3):
    key = _put(s3, BUCKET, REAL_ZIP, recorded=_sha(REAL_ZIP))
    up = S3Uploader(region="eu-west-1")

    assert up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key) is UploadResult.UPLOADED
    assert up.copy(src_bucket=BUCKET, dst_bucket=PROD, key=key) is UploadResult.ALREADY_PRESENT
    head = s3.head_object(Bucket=PROD, Key=key)
    assert head["Metadata"][SHA256_METADATA_KEY] == _sha(REAL_ZIP)


def test_code_sha256_is_the_base64_digest_s3_and_lambda_report():
    import base64

    from repro_lambda.s3_uploader import code_sha256

    assert (
        code_sha256(_sha(REAL_ZIP)) == base64.b64encode(hashlib.sha256(REAL_ZIP).digest()).decode()
    )


def test_reproduce_cache_hits_rebuilds_and_catches_self_asserted_foreign_bytes(repo, s3, docker):
    s3.put_object(
        Bucket=BUCKET,
        Key=_key(repo),
        Body=MARKER,
        Metadata={SHA256_METADATA_KEY: _sha(MARKER)},
    )
    with pytest.raises(ArtifactIntegrityError):
        build_one(
            repo_root=repo,
            spec=SPEC,
            builder=BUILDER,
            bucket=BUCKET,
            catalog=Catalog(lambdas={}),
            source_commit="deadbeef",
            reproduce_cache_hits=True,
        )


def test_reproduced_cache_hit_reports_reproduced_and_records_it_in_the_catalog(repo, s3, docker):
    _build(repo)
    catalog = Catalog(lambdas={})
    out = build_one(
        repo_root=repo,
        spec=SPEC,
        builder=BUILDER,
        bucket=BUCKET,
        catalog=catalog,
        source_commit="deadbeef",
        reproduce_cache_hits=True,
    )
    assert (out.outcome, out.zip_source, out.zip_sha256) == (
        BuildResult.CACHE_HIT,
        "reproduced",
        _sha(REAL_ZIP),
    )
    assert catalog.lambdas["app"].history[0].zip_sha256 == _sha(REAL_ZIP)


def test_unreproduced_cache_hit_is_marked_record_and_not_written_to_the_catalog(repo, s3, docker):
    _build(repo)
    catalog = Catalog(lambdas={})
    out = build_one(
        repo_root=repo,
        spec=SPEC,
        builder=BUILDER,
        bucket=BUCKET,
        catalog=catalog,
        source_commit="deadbeef",
    )
    assert out.zip_source == "record"
    assert catalog.lambdas["app"].history[0].zip_sha256 is None
