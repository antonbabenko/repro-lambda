"""Orchestrate stage -> hash -> probe -> build -> upload -> catalog for one lambda."""

from __future__ import annotations

import enum
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from repro_lambda import __version__
from repro_lambda.catalog import Catalog, CatalogEntry
from repro_lambda.docker_runner import build_python_lambda
from repro_lambda.hasher import compute_content_hash
from repro_lambda.manifest import BuilderConfig, LambdaSpec
from repro_lambda.s3_uploader import S3Uploader, UploadResult
from repro_lambda.source_stager import stage_source


class BuildResult(enum.Enum):
    CACHE_HIT = "cache_hit"
    BUILT_AND_UPLOADED = "built_and_uploaded"
    DRY_RUN = "dry_run"


@dataclass
class BuildOutcome:
    outcome: BuildResult
    sha256: str
    bucket_key: str


def _bucket_for(spec: LambdaSpec, base_bucket: str) -> str:
    if spec.region == "us-east-1":
        return f"{base_bucket}-us-east-1"
    return base_bucket


def compute_sha_for(
    *,
    repo_root: Path,
    spec: LambdaSpec,
    builder: BuilderConfig,
) -> str:
    """Stage source and compute the content hash; tempdir disposed on exit."""
    with tempfile.TemporaryDirectory(prefix="repro-lambda-") as td:
        stage_dir = Path(td)
        stage_source(
            repo_root=repo_root,
            source_dir=spec.source_dir,
            builder=builder,
            stage_dir=stage_dir,
        )
        lock_path = repo_root / spec.resolved_requirements_lock
        if not lock_path.exists():
            raise FileNotFoundError(f"requirements lock not found: {lock_path}")
        return compute_content_hash(
            staged_source_root=stage_dir / "source",
            requirements_lock=lock_path,
            spec=spec,
            base_image=builder.base_image_python,
            builder_version=__version__,
        )


def build_one(
    *,
    repo_root: Path,
    spec: LambdaSpec,
    builder: BuilderConfig,
    bucket: str,
    catalog: Catalog,
    source_commit: str,
    dry_run: bool = False,
) -> BuildOutcome:
    """Build one lambda end-to-end. Returns BuildOutcome with sha + cache verdict."""
    target_bucket = _bucket_for(spec, bucket)

    with tempfile.TemporaryDirectory(prefix="repro-lambda-") as td:
        stage_dir = Path(td)
        stage_source(
            repo_root=repo_root,
            source_dir=spec.source_dir,
            builder=builder,
            stage_dir=stage_dir,
        )
        lock_path = repo_root / spec.resolved_requirements_lock
        sha = compute_content_hash(
            staged_source_root=stage_dir / "source",
            requirements_lock=lock_path,
            spec=spec,
            base_image=builder.base_image_python,
            builder_version=__version__,
        )
        bucket_key = f"lambdas/{spec.logical_name}/{sha}.zip"

        if dry_run:
            return BuildOutcome(BuildResult.DRY_RUN, sha, bucket_key)

        uploader = S3Uploader(region=spec.region)
        if uploader.exists(bucket=target_bucket, key=bucket_key):
            _record(catalog, spec, sha, source_commit, builder)
            return BuildOutcome(BuildResult.CACHE_HIT, sha, bucket_key)

        out_zip = stage_dir / "lambda.zip"
        (stage_dir / "requirements.lock").write_bytes(lock_path.read_bytes())
        build_python_lambda(
            stage_dir=stage_dir,
            out_zip=out_zip,
            base_image=builder.base_image_python,
            arch=spec.arch,
            python_version=spec.runtime.removeprefix("python"),
        )

        result = uploader.upload(bucket=target_bucket, key=bucket_key, body_path=out_zip)
        assert result in {UploadResult.UPLOADED, UploadResult.ALREADY_PRESENT}

        _record(catalog, spec, sha, source_commit, builder)
        return BuildOutcome(BuildResult.BUILT_AND_UPLOADED, sha, bucket_key)


def _record(
    catalog: Catalog,
    spec: LambdaSpec,
    sha: str,
    source_commit: str,
    builder: BuilderConfig,
) -> None:
    catalog.record(
        spec.logical_name,
        CatalogEntry(
            sha256=sha,
            source_commit=source_commit,
            runtime=spec.runtime,
            arch=spec.arch,
            region=spec.region,
            builder_version=__version__,
            base_image_digest=builder.base_image_python.split("@", 1)[-1],
            built_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
