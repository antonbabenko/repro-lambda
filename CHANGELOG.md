# Changelog

## v0.1.0 — 2026-05-27

Initial public release.

### Features

- Python 3.11/3.12/3.13 Lambda packaging with `pip install --require-hashes`
- Byte-reproducible zips via deterministic `zipfile.ZipFile` writes
  (sorted entries, fixed mtime 1980-01-01, 0o755 dirs / 0o644 files / 0o755 executables)
- Content-hash sha256 cache key (source tree + lockfile + spec + base image digest + builder version)
- Idempotent S3 upload via `If-None-Match=*`, designed for bucket-policy-enforced immutability
- `--verify` two-pass byte-reproducibility check
- `--dry-run` for hash + catalog inspection
- `--allow-dirty` for local iteration
- `builds/catalog.json` with bounded 10-entry history per lambda
- Per-arch lockfile generation via `uv pip compile`
- Reusable GitHub Actions workflow at `.github/workflows/build.yml`
- Native arm64 + x86_64 build matrix (no QEMU)

### Not yet supported (planned for v0.2)

- Node.js / npm packaging
- Lambda@Edge-specific constraints (us-east-1 routing, no env vars)
- Rust runtime
