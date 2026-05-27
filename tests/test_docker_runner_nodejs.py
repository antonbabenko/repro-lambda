from pathlib import Path

import pytest

from repro_lambda.docker_runner import (
    ARCH_TO_NPM_CPU,
    DockerRunError,
    build_nodejs_lambda,
    install_nodejs_dependencies,
    pack_in_python_sidecar,
)


def test_arch_to_npm_cpu_maps_arm64_and_x86_64():
    assert ARCH_TO_NPM_CPU["arm64"] == "arm64"
    assert ARCH_TO_NPM_CPU["x86_64"] == "x64"


def test_install_nodejs_dependencies_raises_on_unknown_arch(tmp_path: Path):
    stage_dir = tmp_path / "stage"
    (stage_dir / "source").mkdir(parents=True)
    (stage_dir / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    (stage_dir / "package-lock.json").write_text(
        '{"name": "x", "version": "1.0.0", "lockfileVersion": 3, "requires": true, "packages": {}}'
    )
    out_dir = tmp_path / "pkg"
    with pytest.raises(DockerRunError, match="unsupported arch"):
        install_nodejs_dependencies(
            stage_dir=stage_dir,
            out_pkg_dir=out_dir,
            base_image="x@sha256:0",
            arch="mips",
            node_version="22",
        )


def test_build_nodejs_lambda_orchestrates_install_then_sidecar_pack(tmp_path: Path, mocker):
    stage_dir = tmp_path / "stage"
    (stage_dir / "source").mkdir(parents=True)
    (stage_dir / "source" / "index.js").write_text("exports.handler = async () => ({});")
    (stage_dir / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    (stage_dir / "package-lock.json").write_text(
        '{"name": "x", "version": "1.0.0", "lockfileVersion": 3, "requires": true, "packages": {}}'
    )

    def fake_install(*, out_pkg_dir, **_):
        out_pkg_dir.mkdir(parents=True, exist_ok=True)
        (out_pkg_dir / "index.js").write_text("exports.handler = async () => ({});")

    def fake_sidecar(*, pkg_dir, out_zip, base_image_python, **_):
        out_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    mock_install = mocker.patch(
        "repro_lambda.docker_runner.install_nodejs_dependencies",
        side_effect=fake_install,
    )
    mock_pack = mocker.patch(
        "repro_lambda.docker_runner.pack_in_python_sidecar",
        side_effect=fake_sidecar,
    )

    out = tmp_path / "lambda.zip"
    build_nodejs_lambda(
        stage_dir=stage_dir,
        out_zip=out,
        base_image_nodejs="public.ecr.aws/lambda/nodejs:22@sha256:" + "0" * 64,
        base_image_python="public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64,
        arch="x86_64",
        node_version="22",
    )
    assert out.exists()
    assert out.stat().st_size > 0
    mock_install.assert_called_once()
    mock_pack.assert_called_once()
    pack_kwargs = mock_pack.call_args.kwargs
    assert "python:3.13" in pack_kwargs["base_image_python"]


def test_pack_in_python_sidecar_raises_on_unknown_arch(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    out = tmp_path / "lambda.zip"
    with pytest.raises(DockerRunError, match="unsupported arch"):
        pack_in_python_sidecar(
            pkg_dir=pkg,
            out_zip=out,
            base_image_python="x@sha256:0",
            arch="mips",
        )
