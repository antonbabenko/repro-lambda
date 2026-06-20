from typer.testing import CliRunner

from repro_lambda.cli import app

runner = CliRunner()


def test_cli_version_shows_package_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.2.1" in result.stdout


def test_cli_build_subcommand_exists():
    result = runner.invoke(app, ["build", "--help"])
    assert result.exit_code == 0
    assert "Build" in result.stdout or "build" in result.stdout


def test_cli_lock_subcommand_exists():
    result = runner.invoke(app, ["lock", "--help"])
    assert result.exit_code == 0


def test_cli_init_subcommand_exists():
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0


def test_cli_zip_subcommand_exists():
    result = runner.invoke(app, ["zip", "--help"])
    assert result.exit_code == 0
