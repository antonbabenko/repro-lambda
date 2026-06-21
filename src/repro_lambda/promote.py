"""Promote a built lambda artifact from the dev bucket to the prod bucket.

Promotion is a content-addressed S3 copy: the exact `lambdas/<name>/<sha>.zip`
object already built and verified against the dev bucket is copied byte-for-byte
to the prod bucket. There is no rebuild, so a cross-architecture prod runner can
never produce a different artifact than the one tested in dev.

The sha to promote comes from `builds/catalog.json` (the bounded build history
committed to the source repo), so a promote always targets the artifact the
source commit recorded.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from repro_lambda.build import _bucket_for
from repro_lambda.catalog import load_catalog
from repro_lambda.manifest import LambdaSpec
from repro_lambda.s3_uploader import S3Uploader, UploadResult


class PromoteResult(enum.Enum):
    PROMOTED = "promoted"
    ALREADY_PRESENT = "already_present"
    DRY_RUN = "dry_run"


@dataclass
class PromoteOutcome:
    outcome: PromoteResult
    sha256: str
    bucket_key: str
    src_bucket: str
    dst_bucket: str


class MissingSourceArtifactError(RuntimeError):
    """The dev artifact to promote does not exist (build dev first)."""


class UnknownShaError(RuntimeError):
    """builds/catalog.json has no current sha for the requested lambda."""


def sha_from_catalog(catalog_path: Path, logical_name: str) -> str:
    """Return the current sha recorded for `logical_name`, or raise UnknownShaError."""
    catalog = load_catalog(catalog_path)
    lc = catalog.lambdas.get(logical_name)
    if lc is None or not lc.current:
        raise UnknownShaError(
            f"no catalog entry for {logical_name!r} in {catalog_path}; "
            f"build the dev artifact before promoting"
        )
    return lc.current


def promote_one(
    *,
    spec: LambdaSpec,
    sha: str,
    dev_bucket: str,
    prod_bucket: str,
    uploader: S3Uploader | None = None,
    dry_run: bool = False,
) -> PromoteOutcome:
    """Copy one lambda's `<sha>.zip` from the dev bucket to the prod bucket.

    Lambda@Edge specs (region us-east-1) resolve to the `-us-east-1` bucket
    variant on both sides, matching the build-side key scheme exactly.
    """
    src_bucket = _bucket_for(spec, dev_bucket)
    dst_bucket = _bucket_for(spec, prod_bucket)
    key = f"lambdas/{spec.logical_name}/{sha}.zip"

    if dry_run:
        return PromoteOutcome(PromoteResult.DRY_RUN, sha, key, src_bucket, dst_bucket)

    up = uploader or S3Uploader(region=spec.region)
    if not up.exists(bucket=src_bucket, key=key):
        raise MissingSourceArtifactError(
            f"{spec.logical_name}: source artifact missing at s3://{src_bucket}/{key}"
        )

    result = up.copy(src_bucket=src_bucket, dst_bucket=dst_bucket, key=key)
    outcome = (
        PromoteResult.PROMOTED if result is UploadResult.UPLOADED else PromoteResult.ALREADY_PRESENT
    )
    return PromoteOutcome(outcome, sha, key, src_bucket, dst_bucket)
