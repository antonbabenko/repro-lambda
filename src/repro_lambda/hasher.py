"""Compute the content hash that keys S3 artifacts and decides cache reuse."""

from __future__ import annotations

import hashlib
from pathlib import Path

from repro_lambda.manifest import LambdaSpec, Source


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
    *,
    extra_files: list[tuple[Path, str]] | None = None,
    payload_exec: list[tuple[str, bool]] | None = None,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    sources: tuple[Source, ...] | None = None,
) -> str:
    """
    sha256 over: sorted (relative-path, sha256(content)) tuples for the staged tree
    + sha256(requirements_lock) + spec scalars + base_image + builder_version
    + optional resolved include/exclude filter lists + optional extra_files keyed by
    destination relname (e.g. "package.json").

    Inputs are concatenated with newline separators in a fixed order, then hashed.

    extra_files is a list of (host_path, dest_relname) pairs. The hash covers
    (dest_relname, file_bytes) only - not the host path - so the hash is
    host-path-independent. Callers with no extras produce byte-identical hashes
    to v0.1 (the extras section is omitted entirely when extra_files is falsy).

    include_patterns / exclude_patterns are the RESOLVED per-lambda filter lists.
    They are folded sorted (membership is order-independent, so a pure reorder does
    not re-key) and only when not None, so an explicit empty list (replace-with-empty)
    hashes differently from an unset/None filter. Callers passing None omit the
    section entirely, preserving hashes for code paths that do not resolve a builder.

    sources are the declarative [[lambda.source]] entries. Their RESOLVED metadata
    (the {version}-substituted url/tag/asset/member plus type/repo/extract/dest/
    executable/sha256), sorted by name, folds in - NOT the downloaded bytes, so the
    hash stays download-free. version_from is a lock input and never folded; a
    member/extract/dest/sha256 change re-keys. Omitted entirely when there are none.
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

    if include_patterns is not None:
        h.update(b"---include---\n")
        for pat in sorted(include_patterns):
            h.update(pat.encode("utf-8"))
            h.update(b"\n")
    if exclude_patterns is not None:
        h.update(b"---exclude---\n")
        for pat in sorted(exclude_patterns):
            h.update(pat.encode("utf-8"))
            h.update(b"\n")

    if extra_files:
        h.update(b"---extras---\n")
        # Sort by relname so staging order does not perturb the hash.
        # Key by relname (destination), not host path - this matches the
        # staging contract in source_stager.stage_source(extra_files=...).
        for src, relname in sorted(extra_files, key=lambda pair: pair[1]):
            h.update(relname.encode("utf-8"))
            h.update(b"\x00")
            h.update(_sha256_file(src).encode("ascii"))
            h.update(b"\n")

    # Payload extra_files (prebuilt binaries/trees) are already hashed by content
    # via the staged source tree above; fold in their executable bit here so that
    # flipping +x changes the artifact hash even when bytes are unchanged. Omitted
    # entirely when empty, preserving byte-identical hashes for specs with none.
    if payload_exec:
        h.update(b"---payload-exec---\n")
        for dest, executable in sorted(payload_exec):
            h.update(f"{dest}={int(executable)}\n".encode())

    # Declarative sources: fold resolved metadata only (no bytes), sorted by name.
    # version_from is intentionally excluded (it is a lock input, not artifact identity).
    if sources:
        h.update(b"---sources---\n")
        for s in sorted(sources, key=lambda s: s.name):
            h.update(f"name={s.name}\n".encode())
            h.update(f"type={s.type}\n".encode())
            h.update(f"repo={s.repo}\n".encode())
            h.update(f"url={s.resolved_url}\n".encode())
            h.update(f"tag={s.resolved_tag}\n".encode())
            h.update(f"asset={s.resolved_asset}\n".encode())
            h.update(f"member={s.resolved_member or ''}\n".encode())
            h.update(f"extract={s.extract}\n".encode())
            h.update(f"dest={s.dest}\n".encode())
            h.update(f"executable={int(s.executable)}\n".encode())
            h.update(f"sha256={s.sha256}\n".encode())
            h.update(b"--\n")

    return h.hexdigest()
