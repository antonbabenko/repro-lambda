import shutil
from pathlib import Path

import pytest

from repro_lambda.docker_runner import (
    ARCH_TO_DOCKER_PLATFORM,
    ARCH_TO_PIP_PLATFORM,
    DockerRunError,
    build_python_lambda,
)


def test_arch_mapping_tables_are_complete():
    for arch in ("arm64", "x86_64"):
        assert arch in ARCH_TO_DOCKER_PLATFORM
        assert arch in ARCH_TO_PIP_PLATFORM


def test_arch_mapping_values():
    assert ARCH_TO_DOCKER_PLATFORM["arm64"] == "linux/arm64"
    assert ARCH_TO_DOCKER_PLATFORM["x86_64"] == "linux/amd64"
    assert ARCH_TO_PIP_PLATFORM["arm64"] == "manylinux_2_17_aarch64"
    assert ARCH_TO_PIP_PLATFORM["x86_64"] == "manylinux_2_17_x86_64"


def test_build_python_lambda_raises_on_unknown_arch(tmp_path: Path):
    stage_dir = tmp_path / "stage"
    (stage_dir / "source").mkdir(parents=True)
    (stage_dir / "requirements.lock").write_text("")
    with pytest.raises(DockerRunError, match="unsupported arch"):
        build_python_lambda(
            stage_dir=stage_dir,
            out_zip=tmp_path / "lambda.zip",
            base_image="x@sha256:0",
            arch="mips",
            python_version="3.13",
        )


@pytest.mark.docker
def test_build_python_lambda_produces_a_zip(tmp_path: Path):
    """End-to-end docker invocation. Requires docker daemon."""
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")

    stage_dir = tmp_path / "stage"
    (stage_dir / "source").mkdir(parents=True)
    (stage_dir / "source" / "app.py").write_text(
        "def lambda_handler(event, context):\n    return {'statusCode': 200, 'body': 'ok'}\n"
    )
    (stage_dir / "requirements.lock").write_text("")
    out = tmp_path / "lambda.zip"

    build_python_lambda(
        stage_dir=stage_dir,
        out_zip=out,
        base_image="public.ecr.aws/lambda/python:3.13",
        arch="arm64",
        python_version="3.13",
    )

    assert out.exists()
    assert out.stat().st_size > 0
