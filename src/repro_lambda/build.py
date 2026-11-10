"""Orchestrate stage -> hash -> probe -> build -> upload -> catalog for one lambda."""

from __future__ import annotations

import enum
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from repro_lambda import __version__
from repro_lambda.catalog import Catalog, CatalogEntry
from repro_lambda.docker_runner import build_nodejs_lambda, build_python_lambda
from repro_lambda.hasher import HASH_CONTRACT, compute_content_hash
from repro_lambda.manifest import BuilderConfig, LambdaSpec, resolve_builder
from repro_lambda.s3_uploader import ArtifactIntegrityError, S3Uploader, UploadResult, sha256_hex
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
    # sha256 of the zip bytes behind bucket_key. None for a dry run. This, not the key,
    # is what a pin binds, and only when zip_source is "built" or "reproduced": a
    # "record" value is the object writer's own claim (checked against the body only).
    zip_sha256: str | None = None
    zip_source: str | None = None  # "built" | "reproduced" | "record"


def _bucket_for(spec: LambdaSpec, base_bucket: str) -> str:
    if spec.region == "us-east-1":
        return f"{base_bucket}-us-east-1"
    return base_bucket


def _extras_for(
    spec: LambdaSpec,
    builder: BuilderConfig,
    repo_root: Path,
) -> tuple[str, list[tuple[Path, str]]]:
    """Return (primary_base_image, extras) for the given package_manager."""
    lock_path = repo_root / spec.resolved_requirements_lock
    if spec.package_manager == "pip":
        return builder.base_image_python, [(lock_path, "requirements.lock")]
    if spec.package_manager == "npm":
        package_json_path = repo_root / spec.package_json_resolved
        return builder.base_image_nodejs, [
            (package_json_path, "package.json"),
            (lock_path, "package-lock.json"),
        ]
    raise ValueError(f"unsupported package_manager {spec.package_manager!r}")


def compute_sha_for(
    *,
    repo_root: Path,
    spec: LambdaSpec,
    builder: BuilderConfig,
) -> str:
    """Stage source and compute the content hash; tempdir disposed on exit."""
    builder = resolve_builder(builder, spec)
    with tempfile.TemporaryDirectory(prefix="repro-lambda-") as td:
        stage_dir = Path(td)
        lock_path = repo_root / spec.resolved_requirements_lock
        if not lock_path.exists():
            raise FileNotFoundError(f"requirements lock not found: {lock_path}")

        primary_base_image, extras = _extras_for(spec, builder, repo_root)

        stage_source(
            repo_root=repo_root,
            source_dir=spec.source_dir,
            builder=builder,
            stage_dir=stage_dir,
            extra_files=extras,
            payload_files=list(spec.extra_files),
        )
        return compute_content_hash(
            staged_source_root=stage_dir / "source",
            requirements_lock=lock_path,
            spec=spec,
            base_image=primary_base_image,
            builder_version=HASH_CONTRACT,
            extra_files=extras,
            payload_exec=[(ef.dest, ef.executable) for ef in spec.extra_files],
            include_patterns=builder.include_patterns,
            exclude_patterns=builder.exclude_patterns,
            sources=spec.sources,
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
    sources_token: str | None = None,
    sources_cache: Path | None = None,
    reproduce_cache_hits: bool = False,
) -> BuildOutcome:
    """Build one lambda end-to-end. Returns BuildOutcome with sha + cache verdict.

    ``reproduce_cache_hits`` rebuilds on every cache hit and fails unless the stored
    bytes equal the rebuild, so the reported zip_sha256 is this run's own build. Set it
    on the runs whose output gets pinned.
    """
    builder = resolve_builder(builder, spec)
    target_bucket = _bucket_for(spec, bucket)

    with tempfile.TemporaryDirectory(prefix="repro-lambda-") as td:
        stage_dir = Path(td)
        lock_path = repo_root / spec.resolved_requirements_lock

        # Select primary base image + extras BEFORE staging, in one place per pm.
        primary_base_image, extras = _extras_for(spec, builder, repo_root)

        # Stage source + extras once. Both cache-hit and cache-miss read this tree.
        stage_source(
            repo_root=repo_root,
            source_dir=spec.source_dir,
            builder=builder,
            stage_dir=stage_dir,
            extra_files=extras,
            payload_files=list(spec.extra_files),
        )

        sha = compute_content_hash(
            staged_source_root=stage_dir / "source",
            requirements_lock=lock_path,
            spec=spec,
            base_image=primary_base_image,
            builder_version=HASH_CONTRACT,
            extra_files=extras,
            payload_exec=[(ef.dest, ef.executable) for ef in spec.extra_files],
            include_patterns=builder.include_patterns,
            exclude_patterns=builder.exclude_patterns,
            sources=spec.sources,
        )
        bucket_key = f"lambdas/{spec.logical_name}/{sha}.zip"

        if dry_run:
            return BuildOutcome(BuildResult.DRY_RUN, sha, bucket_key)

        uploader = S3Uploader(region=spec.region)
        # The key is an input hash anyone can compute and the bucket is first-write-wins,
        # so an existing object is never trusted by its key: its body must match the
        # sha256 recorded at first write. With no record (a legacy or foreign object), or
        # when the caller asks to reproduce cache hits, rebuild and compare below.
        compare_sha: str | None = None
        if uploader.exists(bucket=target_bucket, key=bucket_key):
            actual, recorded = uploader.read(bucket=target_bucket, key=bucket_key)
            if recorded is not None and recorded != actual:
                raise ArtifactIntegrityError(
                    f"s3://{target_bucket}/{bucket_key}: body sha256 {actual} != "
                    f"recorded sha256 {recorded}"
                )
            if recorded is not None and not reproduce_cache_hits:
                _record(catalog, spec, sha, source_commit, builder, built=False)
                return BuildOutcome(BuildResult.CACHE_HIT, sha, bucket_key, actual, "record")
            compare_sha = actual

        # Cache miss (or an unrecorded object to compare): fetch + verify + extract
        # declarative sources into the staged tree (post-filter; collides loudly with
        # already-staged source/payload files).
        if spec.sources:
            from repro_lambda.sources import fetch_sources

            fetch_sources(
                sources=spec.sources,
                dest_root=stage_dir / "source",
                cache_dir=sources_cache or (repo_root / "builds" / ".sources-cache"),
                github_token=sources_token,
            )

        out_zip = stage_dir / "lambda.zip"
        if spec.package_manager == "pip":
            build_python_lambda(
                stage_dir=stage_dir,
                out_zip=out_zip,
                base_image=builder.base_image_python,
                arch=spec.arch,
                python_version=spec.runtime.removeprefix("python"),
                compile_bytecode=spec.compile_bytecode,
            )
        else:  # npm
            # nodejs22.x -> "22"; nodejs20.x -> "20"
            node_ver = spec.runtime.removeprefix("nodejs").removesuffix(".x")
            build_nodejs_lambda(
                stage_dir=stage_dir,
                out_zip=out_zip,
                base_image_nodejs=builder.base_image_nodejs,
                base_image_python=builder.base_image_python,
                arch=spec.arch,
                node_version=node_ver,
            )

        built_sha = sha256_hex(out_zip.read_bytes())
        if compare_sha is not None:
            if built_sha != compare_sha:
                raise ArtifactIntegrityError(
                    f"s3://{target_bucket}/{bucket_key}: existing body sha256 "
                    f"{compare_sha} != rebuilt zip sha256 {built_sha}"
                )
            # A missing record cannot be backfilled: the bucket denies any overwrite.
            _record(catalog, spec, sha, source_commit, builder, built=False, zip_sha256=built_sha)
            return BuildOutcome(BuildResult.CACHE_HIT, sha, bucket_key, built_sha, "reproduced")

        result = uploader.upload(bucket=target_bucket, key=bucket_key, body_path=out_zip)
        assert result in {UploadResult.UPLOADED, UploadResult.ALREADY_PRESENT}
        _record(catalog, spec, sha, source_commit, builder, built=True, zip_sha256=built_sha)
        return BuildOutcome(BuildResult.BUILT_AND_UPLOADED, sha, bucket_key, built_sha, "built")


def _record(
    catalog: Catalog,
    spec: LambdaSpec,
    sha: str,
    source_commit: str,
    builder: BuilderConfig,
    *,
    built: bool,
    zip_sha256: str | None = None,
) -> None:
    if spec.package_manager == "npm":
        primary_image = builder.base_image_nodejs
    else:
        primary_image = builder.base_image_python
    catalog.record(
        spec.logical_name,
        CatalogEntry(
            sha256=sha,
            source_commit=source_commit,
            runtime=spec.runtime,
            arch=spec.arch,
            region=spec.region,
            # A cache hit did not build the object, and the key no longer carries the
            # package version, so only the hash contract is known to match it.
            builder_version=__version__ if built else f"hash-contract:{HASH_CONTRACT}",
            base_image_digest=primary_image.split("@", 1)[-1],
            built_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            zip_sha256=zip_sha256,
        ),
    )
