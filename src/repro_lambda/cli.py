"""repro-lambda CLI entrypoint."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from repro_lambda import __version__

app = typer.Typer(
    name="repro-lambda",
    help="Build reproducible AWS Lambda packages outside Terraform.",
    no_args_is_help=True,
)


ARCH_TO_UV_PLATFORM = {
    "arm64": "aarch64-manylinux_2_28",
    "x86_64": "x86_64-manylinux_2_28",
}


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Top-level callback. The --version flag is the only global option."""
    _ = version


@app.command()
def build(
    target: Annotated[str, typer.Argument(help="Lambda logical_name or 'all'.")] = "all",
    manifest: Annotated[
        Path,
        typer.Option("--manifest", "-m", help="Path to lambdas.toml."),
    ] = Path("lambdas.toml"),
    bucket: Annotated[
        str,
        typer.Option(
            "--bucket",
            envvar="REPRO_LAMBDA_BUCKET",
            help="Base S3 bucket name (us-east-1 variant auto-derived).",
        ),
    ] = "",
    verify: Annotated[bool, typer.Option("--verify")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty")] = False,
    arch: Annotated[
        str,
        typer.Option(
            "--arch", help="Only build lambdas with this arch (e.g. arm64, x86_64). Empty = all."
        ),
    ] = "",
    json_out: Annotated[
        Path | None,
        typer.Option(
            "--json-out",
            help="Also write the build summary JSON array to this file (for CI Step Summary).",
        ),
    ] = None,
) -> None:
    """Build one lambda (or all) per manifest and upload to S3."""
    import json

    from repro_lambda.build import build_one
    from repro_lambda.catalog import load_catalog
    from repro_lambda.git_guard import DirtyWorktreeError, ensure_clean_worktree
    from repro_lambda.manifest import load_manifest

    repo_root = manifest.parent.resolve()
    parsed = load_manifest(manifest)

    selected = (
        parsed.lambdas
        if target == "all"
        else [s for s in parsed.lambdas if s.logical_name == target]
    )
    if not selected:
        typer.echo(f"no lambda named {target!r} in {manifest}", err=True)
        raise typer.Exit(2)

    if arch:
        selected = [s for s in selected if s.arch == arch]
        if not selected:
            typer.echo(f"no lambdas with arch {arch!r} in {manifest}; nothing to build")
            raise typer.Exit(0)

    if not dry_run and not bucket:
        typer.echo(
            "--bucket or REPRO_LAMBDA_BUCKET env var is required for non-dry-run",
            err=True,
        )
        raise typer.Exit(2)

    for spec in selected:
        try:
            ensure_clean_worktree(
                repo_root=repo_root,
                source_dir=spec.source_dir,
                allow_dirty=allow_dirty,
            )
        except DirtyWorktreeError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(2) from e

    catalog_path = repo_root / "builds" / "catalog.json"
    catalog = load_catalog(catalog_path)

    try:
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        source_commit = "unknown"

    sources_token = os.environ.get("REPRO_LAMBDA_SOURCES_TOKEN") or None

    summary = []
    for spec in selected:
        outcome = build_one(
            repo_root=repo_root,
            spec=spec,
            builder=parsed.builder,
            bucket=bucket or "dry-run",
            catalog=catalog,
            source_commit=source_commit,
            dry_run=dry_run,
            sources_token=sources_token,
        )
        summary.append(
            {
                "logical_name": spec.logical_name,
                "outcome": outcome.outcome.value,
                "sha256": outcome.sha256,
                "bucket_key": outcome.bucket_key,
            }
        )

    if verify:
        from repro_lambda.verify import ReproducibilityError, verify_reproducible

        for spec in selected:
            try:
                sha_a, _sha_b = verify_reproducible(
                    repo_root=repo_root,
                    spec=spec,
                    builder=parsed.builder,
                )
                typer.echo(f"verify {spec.logical_name}: reproducible (sha={sha_a})")
            except ReproducibilityError as e:
                typer.echo(f"verify {spec.logical_name}: FAILED — {e}", err=True)
                raise typer.Exit(1) from e

    if not dry_run:
        catalog.save(catalog_path)

    if json_out is not None:
        json_out.write_text(json.dumps(summary, indent=2))

    typer.echo(json.dumps(summary, indent=2))


@app.command()
def promote(
    target: Annotated[str, typer.Argument(help="Lambda logical_name or 'all'.")] = "all",
    manifest: Annotated[
        Path,
        typer.Option("--manifest", "-m", help="Path to lambdas.toml."),
    ] = Path("lambdas.toml"),
    dev_bucket: Annotated[
        str,
        typer.Option(
            "--dev-bucket",
            envvar="REPRO_LAMBDA_DEV_BUCKET",
            help="Source (dev) base bucket; us-east-1 variant auto-derived.",
        ),
    ] = "",
    prod_bucket: Annotated[
        str,
        typer.Option(
            "--prod-bucket",
            envvar="REPRO_LAMBDA_PROD_BUCKET",
            help="Destination (prod) base bucket; us-east-1 variant auto-derived.",
        ),
    ] = "",
    sha: Annotated[
        str,
        typer.Option(
            "--sha",
            help="Promote this exact 64-hex content sha instead of the catalog's current. "
            "Requires a single lambda target (not 'all').",
        ),
    ] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Copy built artifacts dev -> prod by content hash (no rebuild).

    By default the sha per lambda is read from builds/catalog.json, so the artifact
    promoted is exactly the one the source commit built and tested in dev. Pass
    ``--sha`` to promote one explicit content sha (e.g. copy-pasted from a build run's
    Step Summary), bypassing the catalog.
    """
    import json
    import re

    from repro_lambda.manifest import load_manifest
    from repro_lambda.promote import (
        MissingSourceArtifactError,
        UnknownShaError,
        promote_one,
        sha_from_catalog,
    )

    repo_root = manifest.parent.resolve()
    parsed = load_manifest(manifest)

    selected = (
        parsed.lambdas
        if target == "all"
        else [s for s in parsed.lambdas if s.logical_name == target]
    )
    if not selected:
        typer.echo(f"no lambda named {target!r} in {manifest}", err=True)
        raise typer.Exit(2)

    if sha:
        if target == "all" or len(selected) != 1:
            typer.echo("--sha requires a single lambda target (not 'all')", err=True)
            raise typer.Exit(2)
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            typer.echo(f"--sha must be 64 lowercase hex chars, got {sha!r}", err=True)
            raise typer.Exit(2)

    if not dry_run and (not dev_bucket or not prod_bucket):
        typer.echo(
            "--dev-bucket and --prod-bucket (or REPRO_LAMBDA_DEV_BUCKET / "
            "REPRO_LAMBDA_PROD_BUCKET) are required for non-dry-run",
            err=True,
        )
        raise typer.Exit(2)

    catalog_path = repo_root / "builds" / "catalog.json"
    summary = []
    for spec in selected:
        try:
            resolved_sha = sha or sha_from_catalog(catalog_path, spec.logical_name)
            outcome = promote_one(
                spec=spec,
                sha=resolved_sha,
                dev_bucket=dev_bucket,
                prod_bucket=prod_bucket,
                dry_run=dry_run,
            )
        except (MissingSourceArtifactError, UnknownShaError) as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        summary.append(
            {
                "logical_name": spec.logical_name,
                "outcome": outcome.outcome.value,
                "sha256": outcome.sha256,
                "bucket_key": outcome.bucket_key,
                "src_bucket": outcome.src_bucket,
                "dst_bucket": outcome.dst_bucket,
            }
        )

    typer.echo(json.dumps(summary, indent=2))


@app.command()
def lock(
    manifest: Annotated[Path, typer.Option("--manifest", "-m")] = Path("lambdas.toml"),
    requirements: Annotated[
        bool,
        typer.Option(
            "--requirements/--no-requirements",
            help="Regenerate per-arch requirements.${arch}.lock via uv pip compile.",
        ),
    ] = True,
    sources: Annotated[
        bool,
        typer.Option(
            "--sources/--no-sources",
            help="Re-resolve version_from + re-pin [[lambda.source]] sha256 in the manifest.",
        ),
    ] = True,
) -> None:
    """Lock pinned inputs: per-arch requirements locks and/or declarative source pins."""
    from repro_lambda.manifest import load_manifest

    parsed = load_manifest(manifest)
    repo_root = manifest.parent.resolve()

    if requirements:
        _lock_requirements(parsed, repo_root)

    if sources:
        from repro_lambda.source_locker import lock_sources

        token = os.environ.get("REPRO_LAMBDA_SOURCES_TOKEN") or None
        changed = lock_sources(manifest, token)
        typer.echo(f"lock sources: {'updated ' + str(manifest) if changed else 'no changes'}")


def _lock_requirements(parsed, repo_root: Path) -> None:
    for spec in parsed.lambdas:
        if spec.package_manager == "npm":
            typer.echo(
                f"skip {spec.logical_name}: npm uses package-lock.json directly "
                f"(regenerate with `npm install` upstream)"
            )
            continue
        requirements_in = repo_root / spec.source_dir / "requirements.in"
        if not requirements_in.exists():
            typer.echo(f"skip {spec.logical_name}: no {requirements_in}", err=True)
            continue
        lock_path = repo_root / spec.resolved_requirements_lock
        uv_platform = ARCH_TO_UV_PLATFORM[spec.arch]
        py_version = spec.runtime.removeprefix("python")
        cmd = [
            "uv",
            "pip",
            "compile",
            str(requirements_in),
            "--python-version",
            py_version,
            "--python-platform",
            uv_platform,
            "--generate-hashes",
            "-o",
            str(lock_path),
        ]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            typer.echo(f"lock {spec.logical_name} failed: {result.stderr}", err=True)
            raise typer.Exit(result.returncode)
        typer.echo(f"lock {spec.logical_name}: wrote {lock_path}")


@app.command()
def init() -> None:
    """Scaffold lambdas.toml and CI caller workflow."""
    typer.echo("init stub")
    raise typer.Exit(0)


# Stripped from every lambda zip so the container build needs no findutils/xargs
# (absent from minimal Lambda base images): Python caches and the non-deterministic
# dist-info metadata files pip writes (RECORD, INSTALLER, direct_url.json, REQUESTED).
_LAMBDA_ZIP_EXCLUDES = [
    "*__pycache__*",
    "*.pyc",
    "*.dist-info/RECORD",
    "*.dist-info/INSTALLER",
    "*.dist-info/direct_url.json",
    "*.dist-info/REQUESTED",
]


def _zip_impl(src: Path, out: Path) -> None:
    """Pack a directory into a deterministic zip (used inside container)."""
    from repro_lambda.zip_packager import pack_directory

    pack_directory(src, out, exclude_glob=_LAMBDA_ZIP_EXCLUDES)


@app.command(name="zip")
def zip_(
    src: Annotated[Path, typer.Option("--src", help="Staged package directory.")],
    out: Annotated[Path, typer.Option("--out", help="Output zip path.")],
) -> None:
    """Pack a directory into a deterministic zip (used inside container)."""
    _zip_impl(src, out)
    raise typer.Exit(0)
