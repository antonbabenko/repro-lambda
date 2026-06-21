import shutil
from pathlib import Path

import pytest

from repro_lambda.docker_runner import (
    ARCH_TO_DOCKER_PLATFORM,
    ARCH_TO_PIP_PLATFORMS,
    DockerRunError,
    build_python_lambda,
    pip_platform_flags,
)


def test_arch_mapping_tables_are_complete():
    for arch in ("arm64", "x86_64"):
        assert arch in ARCH_TO_DOCKER_PLATFORM
        assert arch in ARCH_TO_PIP_PLATFORMS


def test_arch_mapping_values():
    assert ARCH_TO_DOCKER_PLATFORM["arm64"] == "linux/arm64"
    assert ARCH_TO_DOCKER_PLATFORM["x86_64"] == "linux/amd64"


def test_pip_platform_flags_span_the_range_and_cap_at_2_34():
    """Multiple --platform flags so pip picks the best COMPILED wheel per package; a single
    tag silently dropped compiled wheels (e.g. wrapt -> py3-none-any). Cap at glibc 2.34."""
    for arch, tag_arch in (("x86_64", "x86_64"), ("arm64", "aarch64")):
        flags = pip_platform_flags(arch)
        tags = ARCH_TO_PIP_PLATFORMS[arch]
        # Repeated --platform, one per tag, in declared (newest-first) order.
        assert flags == " ".join(f"--platform {t}" for t in tags)
        assert flags.count("--platform ") == len(tags)
        # Must include both the historical floors that each broke one direction.
        assert f"manylinux_2_17_{tag_arch}" in flags  # pydantic-core (2_17-only) still resolves
        assert f"manylinux_2_28_{tag_arch}" in flags  # wrapt's compiled cp313 wheel resolves
        # Never a baseline above the runtime's glibc 2.34 (would be unloadable).
        for n in (35, 36, 38, 40):
            assert f"manylinux_2_{n}_" not in flags


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
