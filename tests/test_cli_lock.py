import subprocess
from pathlib import Path

from typer.testing import CliRunner

from repro_lambda.cli import app

runner = CliRunner()


def _write_min_manifest(repo: Path, arch: str) -> None:
    (repo / "handler").mkdir(exist_ok=True)
    (repo / "handler" / "requirements.in").write_text("")
    (repo / "lambdas.toml").write_text(
        "[[lambda]]\n"
        'logical_name = "app"\n'
        'source_dir = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime = "python3.13"\n'
        f'arch = "{arch}"\n'
        'handler = "app.lambda_handler"\n'
        "[builder]\n"
        'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:' + "0" * 64 + '"\n'
    )


def test_lock_calls_uv_pip_compile_with_correct_platform(tmp_path: Path, mocker):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_min_manifest(repo, "arm64")

    spy = mocker.patch(
        "repro_lambda.cli.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    result = runner.invoke(app, ["lock", "--manifest", str(repo / "lambdas.toml")])
    assert result.exit_code == 0, result.stdout

    uv_calls = [c for c in spy.call_args_list if "uv" in str(c.args[0])]
    assert uv_calls, f"uv pip compile not invoked: {spy.call_args_list}"
    cmd = uv_calls[0].args[0]
    assert "uv" in cmd[0]
    assert "compile" in cmd
    assert "--python-version" in cmd
    assert "3.13" in cmd
    assert "--python-platform" in cmd
    assert "aarch64-manylinux_2_28" in cmd
    assert "--generate-hashes" in cmd


def test_lock_uses_x86_64_platform_when_spec_targets_x86_64(tmp_path: Path, mocker):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_min_manifest(repo, "x86_64")

    spy = mocker.patch(
        "repro_lambda.cli.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    runner.invoke(app, ["lock", "--manifest", str(repo / "lambdas.toml")])
    uv_calls = [c for c in spy.call_args_list if "uv" in str(c.args[0])]
    cmd = uv_calls[0].args[0]
    assert "x86_64-manylinux_2_28" in cmd


def test_lock_skips_npm_specs(tmp_path: Path, mocker):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "handler").mkdir()
    (repo / "lambdas.toml").write_text(
        "[[lambda]]\n"
        'logical_name = "edge"\n'
        'source_dir = "handler"\n'
        'requirements_lock = "handler/package-lock.json"\n'
        'package_json = "handler/package.json"\n'
        'runtime = "nodejs22.x"\n'
        'arch = "x86_64"\n'
        'handler = "index.handler"\n'
        'package_manager = "npm"\n'
        "[builder]\n"
        'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:' + "0" * 64 + '"\n'
        'base_image_nodejs = "public.ecr.aws/lambda/nodejs:22@sha256:' + "0" * 64 + '"\n'
    )
    spy = mocker.patch(
        "repro_lambda.cli.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    result = runner.invoke(app, ["lock", "--manifest", str(repo / "lambdas.toml")])
    assert result.exit_code == 0, result.stdout
    uv_calls = [c for c in spy.call_args_list if "uv" in str(c.args[0])]
    assert not uv_calls, f"uv pip compile should not run for npm specs: {uv_calls}"
    assert "skip edge" in result.stdout
