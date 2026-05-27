"""Compute the content hash that keys S3 artifacts and decides cache reuse."""

from __future__ import annotations

import hashlib
from pathlib import Path

from repro_lambda.manifest import LambdaSpec


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_content_hash(
    staged_source_root: Path,
    requirements_lock: Path,
    spec: LambdaSpec,
    base_image: str,
    builder_version: str,
) -> str:
    """
    sha256 over: sorted (relative-path, sha256(content)) tuples for the staged tree
    + sha256(requirements_lock) + spec scalars + base_image + builder_version.

    Inputs are concatenated with newline separators in a fixed order, then hashed.
    """
    h = hashlib.sha256()

    files = sorted(p for p in staged_source_root.rglob("*") if p.is_file())
    for f in files:
        rel = f.relative_to(staged_source_root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(_sha256_file(f).encode("ascii"))
        h.update(b"\n")

    h.update(b"---\n")
    h.update(_sha256_file(requirements_lock).encode("ascii"))
    h.update(b"\n")

    h.update(f"runtime={spec.runtime}\n".encode())
    h.update(f"arch={spec.arch}\n".encode())
    h.update(f"handler={spec.handler}\n".encode())
    h.update(f"region={spec.region}\n".encode())
    h.update(f"package_manager={spec.package_manager}\n".encode())
    h.update(f"lambda_at_edge={int(spec.lambda_at_edge)}\n".encode())
    h.update(f"hash_extra={spec.hash_extra}\n".encode())
    h.update(f"base_image={base_image}\n".encode())
    h.update(f"builder_version={builder_version}\n".encode())

    return h.hexdigest()
