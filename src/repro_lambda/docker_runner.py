"""Run pip install + cleanup + zip inside a digest-pinned Docker container."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ARCH_TO_DOCKER_PLATFORM: dict[str, str] = {
    "arm64": "linux/arm64",
    "x86_64": "linux/amd64",
}

ARCH_TO_PIP_PLATFORM: dict[str, str] = {
    "arm64": "manylinux_2_28_aarch64",
    "x86_64": "manylinux_2_28_x86_64",
}


class DockerRunError(RuntimeError):
    pass


_CONTAINER_SCRIPT = r"""
set -euxo pipefail
PKG=/build/pkg
mkdir -p "$PKG"

pip install \
  --no-cache-dir --no-compile \
  --require-hashes --only-binary=:all: \
  --platform "$PIP_PLATFORM" \
  --python-version "$PY_VERSION" \
  --implementation cp --abi "cp${PY_VERSION//./}" \
  --target "$PKG" \
  -r /src/requirements.lock

find "$PKG" -depth -type d -name "__pycache__" -exec rm -rf {} +
find "$PKG" -type f -name "*.pyc" -delete
find "$PKG" -type d -name "*.dist-info" -exec sh -c \
  'rm -f "$1/RECORD" "$1/INSTALLER" "$1/direct_url.json"' _ {} \;

cp -R /src/source/. "$PKG/"

python -m repro_lambda zip --src "$PKG" --out /out/lambda.zip
"""


def _builder_module_root() -> Path:
    """Return the on-host directory containing the installed repro_lambda package."""
    import repro_lambda

    return Path(repro_lambda.__file__).parent.parent


def build_python_lambda(
    *,
    stage_dir: Path,
    out_zip: Path,
    base_image: str,
    arch: str,
    python_version: str,
) -> None:
    """
    Docker-run pip install + cleanup + zip in a digest-pinned container, mounting
    the host-staged source and the host-side repro_lambda package read-only.

    Mounts:
      stage_dir         -> /src        (ro)  must contain source/ + requirements.lock
      builder_root      -> /builder    (ro)  PYTHONPATH so `python -m repro_lambda` resolves
      out_dir(out_zip)  -> /out        (rw)  destination for lambda.zip
    """
    if arch not in ARCH_TO_DOCKER_PLATFORM:
        raise DockerRunError(
            f"unsupported arch {arch!r}; supported: {list(ARCH_TO_DOCKER_PLATFORM)}"
        )
    if shutil.which("docker") is None:
        raise DockerRunError("docker CLI not found on PATH")

    docker_platform = ARCH_TO_DOCKER_PLATFORM[arch]
    pip_platform = ARCH_TO_PIP_PLATFORM[arch]

    out_dir = out_zip.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    builder_root = _builder_module_root()

    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        docker_platform,
        "-v",
        f"{stage_dir}:/src:ro",
        "-v",
        f"{builder_root}:/builder:ro",
        "-v",
        f"{out_dir}:/out",
        "-e",
        "PYTHONPATH=/builder",
        "-e",
        f"PIP_PLATFORM={pip_platform}",
        "-e",
        f"PY_VERSION={python_version}",
        "--entrypoint",
        "bash",
        base_image,
        "-euxc",
        _CONTAINER_SCRIPT,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DockerRunError(
            f"docker build failed (exit {result.returncode}):\n"
            f"--- stderr ---\n{result.stderr}\n"
            f"--- stdout ---\n{result.stdout}\n"
        )

    produced = out_dir / "lambda.zip"
    if produced != out_zip:
        produced.rename(out_zip)
