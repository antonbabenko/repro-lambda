"""Two-pass reproducibility check: build twice in isolated stage dirs, compare sha256."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from repro_lambda.docker_runner import build_nodejs_lambda, build_python_lambda
from repro_lambda.manifest import BuilderConfig, LambdaSpec
from repro_lambda.source_stager import stage_source


class ReproducibilityError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extras_for(spec: LambdaSpec, lock_path: Path, repo_root: Path) -> list[tuple[Path, str]]:
    if spec.package_manager == "pip":
        return [(lock_path, "requirements.lock")]
    if spec.package_manager == "npm":
        return [
            (repo_root / spec.package_json_resolved, "package.json"),
            (lock_path, "package-lock.json"),
        ]
    raise ValueError(f"unsupported package_manager {spec.package_manager!r}")


def verify_reproducible(
    *,
    repo_root: Path,
    spec: LambdaSpec,
    builder: BuilderConfig,
) -> tuple[str, str]:
    """
    Run the build twice in independent tempdirs and compare zip sha256.

    Returns (sha_build_1, sha_build_2) on match. Raises ReproducibilityError on mismatch.
    """
    lock_path = repo_root / spec.resolved_requirements_lock
    extras = _extras_for(spec, lock_path, repo_root)

    shas: list[str] = []
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="repro-verify-") as td:
            stage_dir = Path(td)
            stage_source(
                repo_root=repo_root,
                source_dir=spec.source_dir,
                builder=builder,
                stage_dir=stage_dir,
                extra_files=extras,
            )
            out_zip = stage_dir / "lambda.zip"
            if spec.package_manager == "pip":
                build_python_lambda(
                    stage_dir=stage_dir,
                    out_zip=out_zip,
                    base_image=builder.base_image_python,
                    arch=spec.arch,
                    python_version=spec.runtime.removeprefix("python"),
                )
            else:  # npm
                node_ver = spec.runtime.removeprefix("nodejs").removesuffix(".x")
                build_nodejs_lambda(
                    stage_dir=stage_dir,
                    out_zip=out_zip,
                    base_image_nodejs=builder.base_image_nodejs,
                    base_image_python=builder.base_image_python,
                    arch=spec.arch,
                    node_version=node_ver,
                )
            shas.append(_sha256(out_zip))

    if shas[0] != shas[1]:
        raise ReproducibilityError(f"two builds produced different zips: {shas[0]} vs {shas[1]}")
    return shas[0], shas[1]
