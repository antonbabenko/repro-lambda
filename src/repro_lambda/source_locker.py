"""`lock` for declarative sources: resolve version_from, re-pin sha256, rewrite toml.

This is the ONLY code that resolves a source's version and recomputes its sha256. It
runs deliberately (a human or a scheduled job), never on the build path. The rewrite is
comment-preserving (tomlkit) and atomic; a run that changes nothing leaves the file
untouched and reports no change (so a scheduled job opens no empty PR).
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

import tomlkit

from repro_lambda.manifest import Source, load_manifest
from repro_lambda.sources import (
    SourceFetchError,
    download_unverified,
    extract_to_temp,
)

_MAX_VERSION_FILE_BYTES = 1024 * 1024  # a pin file is tiny; cap to be safe


@dataclass
class SourcePin:
    """The re-resolved pin for one source (what lock writes back)."""

    name: str
    sha256: str
    version: str  # "" when the source has no version_from
    changed: bool


def _read_pin_file(path: Path) -> str:
    """Read a version_from pin file, with the guards both readers share."""
    if not path.is_file():
        raise SourceFetchError(f"version_from file not found: {path}")
    if path.stat().st_size > _MAX_VERSION_FILE_BYTES:
        raise SourceFetchError(f"version_from file too large: {path}")
    return path.read_text(encoding="utf-8")


def _read_asdf_version(path: Path, key: str) -> str:
    """Read `<key> <value>` from an asdf-style file (e.g. .tool-versions)."""
    for raw in _read_pin_file(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] == key:
            return parts[1]
    raise SourceFetchError(f"key {key!r} not found in {path.name}")


def _mise_version(spec: object) -> str:
    """The pinned version of one `[tools]` entry, or "" when it pins none.

    mise accepts a bare string, a table/inline table with `version`, or a list of
    versions (first wins). An entry with only e.g. `bin_path` pins nothing.
    """
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        version = spec.get("version")
        return version if isinstance(version, str) else ""
    if isinstance(spec, list):
        return next((v for v in spec if isinstance(v, str)), "")
    return ""


def _read_mise_version(path: Path, key: str) -> str:
    """Read a tool version from a mise config's `[tools]` table.

    `key` matches either the full tool key or its short name, so
    "aqua:hashicorp/terraform" and "http:hcledit" both resolve as "terraform"/"hcledit".
    """
    try:
        tools = tomllib.loads(_read_pin_file(path)).get("tools", {})
    except tomllib.TOMLDecodeError as exc:
        raise SourceFetchError(f"version_from file is not valid TOML: {path} ({exc})") from exc
    for raw_key, spec in tools.items():
        if key not in (raw_key, raw_key.split(":", 1)[-1].split("/")[-1]):
            continue
        version = _mise_version(spec)
        if version:
            return version
    raise SourceFetchError(f"key {key!r} not found in {path.name}")


_VERSION_READERS = {"mise": _read_mise_version, "asdf": _read_asdf_version}


def _ordered(sources: tuple[Source, ...]) -> list[Source]:
    """Non-version_from sources first (the referenced roots), then dependents.

    version_from is single-level (validated at load), so this two-pass order guarantees a
    referenced source is fetched + extracted before any source that reads its version.
    """
    return [s for s in sources if s.version_from is None] + [
        s for s in sources if s.version_from is not None
    ]


def _lock_lambda_sources(
    sources: tuple[Source, ...], token: str | None, tmp: Path
) -> dict[str, SourcePin]:
    """Re-resolve + re-pin every source of one lambda. Returns pins keyed by name."""
    tmp.mkdir(parents=True, exist_ok=True)
    src_by_name = {s.name: s for s in sources}
    pins: dict[str, SourcePin] = {}
    extracted: dict[str, Path] = {}
    for src in _ordered(sources):
        version = src.version
        if src.version_from is not None:
            ref = src.version_from.source
            if ref not in extracted:
                raise SourceFetchError(
                    f"source {src.name!r}: referenced source {ref!r} was not extracted "
                    f"(it must declare an archive extract)"
                )
            # Read relative to the referenced source's member-stripped tree, so file= can
            # be e.g. "mise.toml" rather than the version-dependent "pofix-9.9/...".
            base = extracted[ref]
            ref_member = src_by_name[ref].resolved_member
            if ref_member:
                base = base / ref_member
            read = _VERSION_READERS[src.version_from.format]
            version = read(base / src.version_from.file, src.version_from.key)
        resolved = replace(src, version=version)

        raw = tmp / f"lock-{src.name}"
        new_sha = download_unverified(resolved, token, raw)

        if resolved.extract != "none":
            extracted[src.name] = extract_to_temp(raw, resolved.extract, tmp / f"x-{src.name}")

        changed = new_sha != src.sha256 or version != src.version
        pins[src.name] = SourcePin(
            name=src.name,
            sha256=new_sha,
            version=version if src.version_from is not None else "",
            changed=changed,
        )
    return pins


def _apply_pins(doc: tomlkit.TOMLDocument, all_pins: list[dict[str, SourcePin]]) -> None:
    """Write the re-pinned sha256 (+ version for version_from sources) into the toml doc."""
    lambdas = doc.get("lambda", [])
    for li, lam in enumerate(lambdas):
        pins = all_pins[li]
        for st in lam.get("source", []):
            pin = pins.get(st["name"])
            if pin is None:
                continue
            st["sha256"] = pin.sha256
            if pin.version:  # only version_from sources carry a locked version
                st["version"] = pin.version


def lock_sources(manifest_path: Path, github_token: str | None) -> bool:
    """Re-resolve + re-pin all sources across the manifest. Returns True if it changed.

    A False return means every pin was already current: the file is left byte-for-byte
    unchanged (idempotent; no spurious PR from a scheduled run).
    """
    parsed = load_manifest(manifest_path)
    if not any(spec.sources for spec in parsed.lambdas):
        return False

    all_pins: list[dict[str, SourcePin]] = []
    with tempfile.TemporaryDirectory(prefix="repro-lock-") as td:
        tmp = Path(td)
        for spec in parsed.lambdas:
            all_pins.append(
                _lock_lambda_sources(spec.sources, github_token, tmp / spec.logical_name)
                if spec.sources
                else {}
            )

    if not any(pin.changed for pins in all_pins for pin in pins.values()):
        return False

    doc = tomlkit.parse(manifest_path.read_text(encoding="utf-8"))
    _apply_pins(doc, all_pins)
    staging = manifest_path.with_name(manifest_path.name + ".lock.tmp")
    staging.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(staging, manifest_path)
    return True
