"""builds/catalog.json — bounded per-lambda build history committed to source repo."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
MAX_HISTORY = 10


@dataclass
class CatalogEntry:
    sha256: str
    source_commit: str
    runtime: str
    arch: str
    region: str
    builder_version: str
    base_image_digest: str
    built_at: str  # ISO-8601 UTC, e.g. "2026-05-27T09:30:00Z"
    # sha256 of the zip bytes, set only when this run built or reproduced them (never
    # from an object's writer-set record). promote checks the artifact against it.
    zip_sha256: str | None = None


@dataclass
class LambdaCatalog:
    current: str
    history: list[CatalogEntry] = field(default_factory=list)


@dataclass
class Catalog:
    lambdas: dict[str, LambdaCatalog]

    def record(self, logical_name: str, entry: CatalogEntry) -> None:
        lc = self.lambdas.get(logical_name)
        if lc is None:
            self.lambdas[logical_name] = LambdaCatalog(current=entry.sha256, history=[entry])
            return
        if lc.current == entry.sha256:
            # Same artifact: keep its first entry, but learn the reproduced zip sha256.
            if entry.zip_sha256 and lc.history and not lc.history[0].zip_sha256:
                lc.history[0].zip_sha256 = entry.zip_sha256
            return
        lc.history.insert(0, entry)
        lc.current = entry.sha256
        lc.history = lc.history[:MAX_HISTORY]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "lambdas": {
                name: {
                    "current": lc.current,
                    "history": [asdict(e) for e in lc.history],
                }
                for name, lc in self.lambdas.items()
            },
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def zip_sha256_for(catalog: Catalog, logical_name: str, sha: str) -> str | None:
    """The zip sha256 a build of this repo recorded for one artifact key, if any."""
    lc = catalog.lambdas.get(logical_name)
    for e in lc.history if lc else []:
        if e.sha256 == sha and e.zip_sha256:
            return e.zip_sha256
    return None


def load_catalog(path: Path) -> Catalog:
    if not path.exists():
        return Catalog(lambdas={})
    raw = json.loads(path.read_text())
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported schema_version {raw.get('schema_version')!r}")
    lambdas: dict[str, LambdaCatalog] = {}
    for name, lc in raw.get("lambdas", {}).items():
        history = [CatalogEntry(**e) for e in lc.get("history", [])]
        lambdas[name] = LambdaCatalog(current=lc["current"], history=history)
    return Catalog(lambdas=lambdas)
