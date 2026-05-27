import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repro_lambda.cli import app

runner = CliRunner()


@pytest.fixture
def consumer_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    (repo / "handler").mkdir()
    (repo / "handler" / "app.py").write_text("def lambda_handler(e, c): return 'ok'\n")
    (repo / "handler" / "requirements.in").write_text("")
    (repo / "handler" / "requirements.arm64.lock").write_text("")
    (repo / "lambdas.toml").write_text(
        "[[lambda]]\n"
        'logical_name = "app"\n'
        'source_dir = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime = "python3.13"\n'
        'arch = "arm64"\n'
        'handler = "app.lambda_handler"\n'
        "[builder]\n"
        'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:' + "0" * 64 + '"\n'
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_verify_passes_when_two_builds_produce_identical_zips(consumer_repo: Path, mocker):
    def fake_build(*, stage_dir, out_zip, **_):
        out_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    mocker.patch("repro_lambda.build.build_python_lambda", side_effect=fake_build)
    mocker.patch("repro_lambda.verify.build_python_lambda", side_effect=fake_build)

    result = runner.invoke(
        app,
        [
            "build",
            "app",
            "--manifest",
            str(consumer_repo / "lambdas.toml"),
            "--verify",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "reproducible" in result.stdout.lower()


def test_verify_fails_when_two_builds_produce_different_zips(consumer_repo: Path, mocker):
    calls = {"n": 0}

    def fake_build(*, stage_dir, out_zip, **_):
        calls["n"] += 1
        out_zip.write_bytes(b"PK\x05\x06" + bytes([calls["n"] % 256]) * 18)

    mocker.patch("repro_lambda.verify.build_python_lambda", side_effect=fake_build)

    result = runner.invoke(
        app,
        [
            "build",
            "app",
            "--manifest",
            str(consumer_repo / "lambdas.toml"),
            "--verify",
            "--dry-run",
        ],
    )
    assert result.exit_code != 0


def test_verify_nodejs_uses_nodejs_builder(tmp_path: Path, mocker):
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    (repo / "handler").mkdir()
    (repo / "handler" / "index.js").write_text(
        "exports.handler = async () => ({statusCode: 200});\n"
    )
    (repo / "handler" / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    (repo / "handler" / "package-lock.json").write_text(
        '{"name": "x", "version": "1.0.0", "lockfileVersion": 3, "requires": true, "packages": {}}'
    )
    (repo / "lambdas.toml").write_text(
        '[[lambda]]\n'
        'logical_name = "edge"\n'
        'source_dir = "handler"\n'
        'requirements_lock = "handler/package-lock.json"\n'
        'package_json = "handler/package.json"\n'
        'runtime = "nodejs22.x"\n'
        'arch = "x86_64"\n'
        'handler = "index.handler"\n'
        'region = "us-east-1"\n'
        'package_manager = "npm"\n'
        'lambda_at_edge = true\n'
        '[builder]\n'
        'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:' + "0" * 64 + '"\n'
        'base_image_nodejs = "public.ecr.aws/lambda/nodejs:22@sha256:' + "0" * 64 + '"\n'
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    def fake_nodejs(*, stage_dir, out_zip, **_):
        out_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    mock_python = mocker.patch("repro_lambda.verify.build_python_lambda")
    mock_nodejs = mocker.patch(
        "repro_lambda.verify.build_nodejs_lambda", side_effect=fake_nodejs
    )
    mocker.patch("repro_lambda.build.build_nodejs_lambda", side_effect=fake_nodejs)

    result = runner.invoke(
        app,
        [
            "build", "edge",
            "--manifest", str(repo / "lambdas.toml"),
            "--verify", "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stdout
    mock_python.assert_not_called()
    # verify_reproducible builds twice; build_one builds once = 3 total via the fake
    assert mock_nodejs.call_count >= 2
    pack_kwargs = mock_nodejs.call_args.kwargs
    assert "nodejs:22" in pack_kwargs["base_image_nodejs"]
    assert "python:3.13" in pack_kwargs["base_image_python"]
