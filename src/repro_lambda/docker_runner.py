"""Run pip install + cleanup + zip inside a digest-pinned Docker container."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ARCH_TO_DOCKER_PLATFORM: dict[str, str] = {
    "arm64": "linux/arm64",
    "x86_64": "linux/amd64",
}

# The AWS Lambda base images (Amazon Linux 2023) ship glibc 2.34, so the runtime can load
# any manylinux wheel up to manylinux_2_34. pip's explicit --platform matches (close to)
# that exact tag, so a SINGLE platform misses wheels tagged with other baselines in both
# directions: a 2_28 floor misses 2_17-only wheels (e.g. pydantic-core), and a 2_17 floor
# misses higher-baseline compiled wheels (e.g. wrapt ships cp313 only as manylinux_2_28 ->
# pip silently falls back to py3-none-any, which broke aws-xray-sdk's runtime boto3 patching
# and 500'd a Lambda). A single floor cannot satisfy both. Pass the FULL compatible range as
# repeated --platform flags, newest -> oldest, capped at 2_34 (the runtime's glibc): pip then
# selects the most-specific COMPILED wheel each package offers and only falls back to
# py3-none-any when no compiled wheel exists. Never list a baseline ABOVE 2_34 - that would
# let pip pick a wheel the runtime cannot load. (arm64 manylinux starts at 2014/2_17; the
# manylinux1/2010/2_5 legacy aliases are x86_64-only.)
ARCH_TO_PIP_PLATFORMS: dict[str, list[str]] = {
    "x86_64": [
        "manylinux_2_34_x86_64",
        "manylinux_2_28_x86_64",
        "manylinux_2_24_x86_64",
        "manylinux2014_x86_64",
        "manylinux_2_17_x86_64",
        "manylinux_2_12_x86_64",
        "manylinux2010_x86_64",
        "manylinux_2_5_x86_64",
        "manylinux1_x86_64",
    ],
    "arm64": [
        "manylinux_2_34_aarch64",
        "manylinux_2_28_aarch64",
        "manylinux_2_24_aarch64",
        "manylinux2014_aarch64",
        "manylinux_2_17_aarch64",
    ],
}


def pip_platform_flags(arch: str) -> str:
    """Repeated ``--platform <tag>`` flags (space-joined) for an arch's manylinux range.

    Word-split UNQUOTED into the pip command in the container so each tag becomes its own
    flag. pip treats a wheel as compatible with ANY listed platform and prefers the
    earliest (newest baseline) match, so compiled wheels win over the py3-none-any fallback.
    """
    return " ".join(f"--platform {tag}" for tag in ARCH_TO_PIP_PLATFORMS[arch])


ARCH_TO_NPM_CPU: dict[str, str] = {
    "arm64": "arm64",
    "x86_64": "x64",
}

# Invariance: keys must match across all arch lookup tables. Adding a new arch
# to one without the other would cause install_nodejs_dependencies to raise
# KeyError instead of DockerRunError. Caught at import time.
assert set(ARCH_TO_DOCKER_PLATFORM) == set(ARCH_TO_PIP_PLATFORMS) == set(ARCH_TO_NPM_CPU), (
    "arch lookup tables must share the same key set; "
    f"DOCKER={set(ARCH_TO_DOCKER_PLATFORM)} PIP={set(ARCH_TO_PIP_PLATFORMS)} NPM={set(ARCH_TO_NPM_CPU)}"
)


class DockerRunError(RuntimeError):
    pass


_PYTHON_INSTALL_SCRIPT = r"""
set -euxo pipefail
PKG=/tmp/build/pkg
mkdir -p "$PKG"
cp -R /src/source/. "$PKG/"

pip install \
  --no-cache-dir --no-compile --require-hashes --only-binary=:all: \
  $PIP_PLATFORM_FLAGS \
  --abi "$PIP_ABI" \
  --python-version "$PIP_PYVER" \
  --implementation cp \
  --target "$PKG" \
  --requirement /src/requirements.lock

# Byte-output cleanup (caches + non-deterministic dist-info metadata) happens in the
# Python zip step below, so this script needs no findutils/xargs (both absent from the
# minimal AWS Lambda base images).
python3 -m repro_lambda zip --src "$PKG" --out /out/lambda.zip
"""

_NODEJS_INSTALL_SCRIPT = r"""
set -euxo pipefail
PKG=/out/pkg
mkdir -p "$PKG"
cp -R /src/source/. "$PKG/"
cp /src/package.json "$PKG/package.json"
cp /src/package-lock.json "$PKG/package-lock.json"
cd "$PKG"

export HOME=/tmp
export NPM_CONFIG_CACHE=/tmp/.npm
export NODE_OPTIONS=--no-warnings

npm ci \
  --omit=dev --ignore-scripts \
  --no-audit --no-fund \
  --cpu="$NPM_CPU" --os=linux

if [ -d "$PKG/node_modules" ]; then
  find "$PKG/node_modules" -type d -name ".bin" -prune -exec sh -c 'for d; do rm -rf -- "$d"; done' _ {} +
  find "$PKG/node_modules" -type f -name "*.md" -delete
  find "$PKG/node_modules" -type f -name "*.markdown" -delete
  find "$PKG/node_modules" -type f -iname "LICENSE*" -delete
  find "$PKG/node_modules" -type f -iname "CHANGELOG*" -delete
fi
"""

_SIDECAR_PACK_SCRIPT = r"""
set -euxo pipefail
python3 -m repro_lambda zip --src /in --out /out/lambda.zip
"""


def _builder_module_root() -> Path:
    """Return the on-host directory containing the installed repro_lambda package."""
    import repro_lambda

    return Path(repro_lambda.__file__).parent.parent


def _docker_user_args() -> list[str]:
    if sys.platform == "win32":
        return []
    import os

    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def _run_docker(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DockerRunError(
            f"docker run failed (exit {result.returncode}):\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"--- stderr ---\n{result.stderr}\n"
            f"--- stdout ---\n{result.stdout}\n"
        )


def build_python_lambda(
    *,
    stage_dir: Path,
    out_zip: Path,
    base_image: str,
    arch: str,
    python_version: str,
) -> None:
    """v0.1-compatible: install + pack inside the Python container."""
    if arch not in ARCH_TO_PIP_PLATFORMS:
        raise DockerRunError(f"unsupported arch {arch!r}")
    if shutil.which("docker") is None:
        raise DockerRunError("docker CLI not found on PATH")

    out_dir = out_zip.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    builder_root = _builder_module_root()

    pyver = python_version  # e.g. "3.13"
    pyver_compact = pyver.replace(".", "")  # "313"

    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        ARCH_TO_DOCKER_PLATFORM[arch],
        *_docker_user_args(),
        "-v",
        f"{stage_dir}:/src:ro",
        "-v",
        f"{builder_root}:/builder:ro",
        "-v",
        f"{out_dir}:/out",
        "-e",
        "PYTHONPATH=/builder",
        "-e",
        f"PIP_PLATFORM_FLAGS={pip_platform_flags(arch)}",
        "-e",
        f"PIP_ABI=cp{pyver_compact}",
        "-e",
        f"PIP_PYVER={pyver}",
        "--entrypoint",
        "bash",
        base_image,
        "-euxc",
        _PYTHON_INSTALL_SCRIPT,
    ]
    _run_docker(cmd)

    produced = out_dir / "lambda.zip"
    if produced != out_zip:
        produced.rename(out_zip)


def install_nodejs_dependencies(
    *,
    stage_dir: Path,
    out_pkg_dir: Path,
    base_image: str,
    arch: str,
    node_version: str,
) -> None:
    if arch not in ARCH_TO_DOCKER_PLATFORM:
        raise DockerRunError(f"unsupported arch {arch!r}")
    if shutil.which("docker") is None:
        raise DockerRunError("docker CLI not found on PATH")

    out_pkg_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        ARCH_TO_DOCKER_PLATFORM[arch],
        *_docker_user_args(),
        "-v",
        f"{stage_dir}:/src:ro",
        "-v",
        f"{out_pkg_dir.parent}:/out",
        "-e",
        f"NPM_CPU={ARCH_TO_NPM_CPU[arch]}",
        "-e",
        f"NODE_VERSION={node_version}",
        "--entrypoint",
        "bash",
        base_image,
        "-euxc",
        _NODEJS_INSTALL_SCRIPT,
    ]
    _run_docker(cmd)


def pack_in_python_sidecar(
    *,
    pkg_dir: Path,
    out_zip: Path,
    base_image_python: str,
    arch: str,
) -> None:
    """Pack `pkg_dir` to `out_zip` inside the digest-pinned Python base image.

    The Python image's zlib is the only deflate implementation invoked, so
    macOS arm64 hosts and Linux x86_64 CI produce byte-identical output.
    """
    if arch not in ARCH_TO_DOCKER_PLATFORM:
        raise DockerRunError(f"unsupported arch {arch!r}")
    if shutil.which("docker") is None:
        raise DockerRunError("docker CLI not found on PATH")

    out_dir = out_zip.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    builder_root = _builder_module_root()

    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        ARCH_TO_DOCKER_PLATFORM[arch],
        *_docker_user_args(),
        "-v",
        f"{pkg_dir}:/in:ro",
        "-v",
        f"{builder_root}:/builder:ro",
        "-v",
        f"{out_dir}:/out",
        "-e",
        "PYTHONPATH=/builder",
        "--entrypoint",
        "bash",
        base_image_python,
        "-euxc",
        _SIDECAR_PACK_SCRIPT,
    ]
    _run_docker(cmd)

    produced = out_dir / "lambda.zip"
    if produced != out_zip:
        produced.rename(out_zip)


def build_nodejs_lambda(
    *,
    stage_dir: Path,
    out_zip: Path,
    base_image_nodejs: str,
    base_image_python: str,
    arch: str,
    node_version: str,
) -> None:
    """Two-container build: Node install + Python-sidecar pack."""
    pkg = out_zip.parent / "pkg"
    install_nodejs_dependencies(
        stage_dir=stage_dir,
        out_pkg_dir=pkg,
        base_image=base_image_nodejs,
        arch=arch,
        node_version=node_version,
    )
    pack_in_python_sidecar(
        pkg_dir=pkg,
        out_zip=out_zip,
        base_image_python=base_image_python,
        arch=arch,
    )
