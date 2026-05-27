"""repro-lambda CLI entrypoint."""

from pathlib import Path
from typing import Annotated

import typer

from repro_lambda import __version__

app = typer.Typer(
    name="repro-lambda",
    help="Build reproducible AWS Lambda packages outside Terraform.",
    no_args_is_help=True,
)


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
    _ = version  # consumed by the callback


@app.command()
def build(
    target: Annotated[str, typer.Argument(help="Lambda logical_name or 'all'.")] = "all",
    manifest: Annotated[
        Path,
        typer.Option("--manifest", "-m", help="Path to lambdas.toml."),
    ] = Path("lambdas.toml"),
    verify: Annotated[bool, typer.Option("--verify", help="Two-pass repro check.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Compute hash, no upload.")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty")] = False,
) -> None:
    """Build one lambda (or all) per manifest and upload to S3."""
    typer.echo(
        f"build stub: target={target} manifest={manifest} "
        f"verify={verify} dry_run={dry_run} allow_dirty={allow_dirty}"
    )
    raise typer.Exit(0)


@app.command()
def lock(
    manifest: Annotated[Path, typer.Option("--manifest", "-m")] = Path("lambdas.toml"),
) -> None:
    """Regenerate per-arch lockfiles from requirements.in."""
    typer.echo(f"lock stub: manifest={manifest}")
    raise typer.Exit(0)


@app.command()
def init() -> None:
    """Scaffold lambdas.toml and CI caller workflow."""
    typer.echo("init stub")
    raise typer.Exit(0)


def _zip_impl(src: Path, out: Path) -> None:
    """Pack a directory into a deterministic zip (used inside container)."""
    typer.echo(f"zip stub: src={src} out={out}")


@app.command(name="zip")
def zip_(
    src: Annotated[Path, typer.Option("--src", help="Staged package directory.")],
    out: Annotated[Path, typer.Option("--out", help="Output zip path.")],
) -> None:
    """Pack a directory into a deterministic zip (used inside container)."""
    _zip_impl(src, out)
    raise typer.Exit(0)
