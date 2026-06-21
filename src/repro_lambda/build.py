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
from repro_lambda.hasher import compute_content_hash
from repro_lambda.manifest import BuilderConfig, LambdaSpec, resolve_builder
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
            builder_version=__version__,
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
) -> BuildOutcome:
    """Build one lambda end-to-end. Returns BuildOutcome with sha + cache verdict."""
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
            builder_version=__version__,
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
        if uploader.exists(bucket=target_bucket, key=bucket_key):
            _record(catalog, spec, sha, source_commit, builder)
            return BuildOutcome(BuildResult.CACHE_HIT, sha, bucket_key)

        # Cache miss only: fetch + verify + extract declarative sources into the staged
        # tree (post-filter; collides loudly with already-staged source/payload files).
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
            builder_version=__version__,
            base_image_digest=primary_image.split("@", 1)[-1],
            built_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
