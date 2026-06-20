# Changelog

## v0.2.2 - 2026-06-20

### Changed
- Reusable workflow `build.yml` now takes `aws-dev-role-arn` and `aws-prod-role-arn` as **inputs** instead of **secrets**. A role ARN is not sensitive (the security boundary is the OIDC trust policy plus the key-level bucket immutability policy), and typing it as a secret blocked callers from passing a derivable literal ARN, since secret inputs reject plain literal values. No package code change: PyPI 0.2.2 is behaviorally identical to 0.2.1.
- Artifact bucket names are now `dev-bucket` / `prod-bucket` **inputs** instead of hardcoded values, so the reusable workflow is consumer-agnostic and carries no environment-specific bucket names.

### Consumer migration
- Bump the workflow ref to `uses: antonbabenko/repro-lambda/.github/workflows/build.yml@v0.2.2` and move `aws-dev-role-arn` / `aws-prod-role-arn` to the `with:` block, adding `dev-bucket` (and `prod-bucket` if you upload to prod). They are inputs now, so plain literals are valid:

      with:
        aws-dev-role-arn: arn:aws:iam::<account>:role/<role>
        dev-bucket: <env>-my-lambda-artifacts

## v0.2.1 - 2026-05-27

### Changed
- CI workflow: `uvx --from "repro-lambda==<v>" repro-lambda <args>` replaces `uv pip install --system "repro-lambda==<v>"`. uv 0.11+ deprecates the `uv pip` legacy interface for install/uninstall/sync.

### Docs
- README install instruction switches to `uv tool install repro-lambda` (plus `uvx repro-lambda` ephemeral alternative).
- SETUP.md examples bumped to `@v0.2.1` / `repro_lambda_version: "0.2.1"`.

### Consumer migration
- Consumer repos must bump their workflow ref to `uses: antonbabenko/repro-lambda/.github/workflows/build.yml@v0.2.1` to receive the uvx-based install. The v0.2.0 workflow ref still works but invokes the deprecated install command.

## v0.2.0 - 2026-05-27

### Added
- Node.js Lambda packaging (`nodejs20.x`, `nodejs22.x`) via `npm ci --omit=dev --ignore-scripts --cpu=${arch} --os=linux` in the digest-pinned Node base image.
- Two-container Node build: install in the Node base image, pack the resulting `pkg/` directory inside the digest-pinned Python base image (the Python image's zlib is the only deflate implementation invoked, so macOS arm64 hosts and Linux x86_64 CI produce byte-identical output).
- `BuilderConfig.base_image_nodejs` (required when any lambda uses `package_manager = "npm"`).
- `LambdaSpec.package_json` (required for npm specs) + `package_json_resolved` property.
- `ARCH_TO_NPM_CPU` mapping (`arm64` -> `arm64`, `x86_64` -> `x64`).
- `build_nodejs_lambda` + `install_nodejs_dependencies` + `pack_in_python_sidecar` in `docker_runner`.
- `stage_source(... extra_files=[(host_path, dest_relname), ...])` for staging artifacts outside `source_dir` (e.g. `package.json`, `package-lock.json`).
- `compute_content_hash(... extra_files=...)` keyed by destination relname so npm `package.json` edits bump the cache key.
- SETUP.md sections for Node.js + Lambda@Edge usage + caveats.
- Docker-gated end-to-end Node.js reproducibility test (`test_e2e_nodejs_lambda.py`) using a `tslib@2.7.0` fixture.
- Python byte-compat regression test (`test_python_byte_compat_regression.py`) pinning v0.1 zip output against a digest-pinned base image.

### Changed
- `build.py` and `verify.py` route per `spec.package_manager` (pip | npm). Both pre-stage source + extras BEFORE the cache-key hash so cache-hit and cache-miss branches see the same hash inputs.
- `manifest.py` accepts `nodejs20.x` and `nodejs22.x` runtimes and `package_manager = "npm"`.
- `lock` subcommand skips npm specs (npm uses package-lock.json directly; regenerate with `npm install` upstream).
- `zip_packager.pack_directory` skips symlinks with a stderr warning (zip cannot preserve link semantics).
- Docker `--user $(id -u):$(id -g)` on POSIX (skipped on Windows; `sys.platform == "win32"` gate).
- `_PYTHON_INSTALL_SCRIPT` now declares the full pip platform quartet (`--platform`, `--abi`, `--python-version`, `--implementation`) and strips `REQUESTED` files alongside `RECORD` / `INSTALLER` / `direct_url.json`. Module-level invariance assert keeps `ARCH_TO_DOCKER_PLATFORM` / `ARCH_TO_PIP_PLATFORM` / `ARCH_TO_NPM_CPU` keys in lockstep.

### Compatibility
- v0.1 zip byte-output preserved (regression-tested by `test_python_byte_compat_regression.py` once the operator records the v0.1.0 reference sha against the pinned base image digest).
- Content-hash key shifts ONCE at the v0.1 -> v0.2 cut-line because the lockfile is now hashed via the unified `extra_files` channel. v0.1-built zips remain pullable by their old keys (content-addressed S3 storage), so the shift only affects the next rebuild.

### Known caveats
- npm workspaces NOT supported (single `package.json` per Lambda only).
- Native deps must ship a `linux-${arch}` binary via `optionalDependencies` in `package-lock.json`; npm cannot cross-compile native modules.
- Symlinks inside `source_dir` are skipped (zip cannot preserve link semantics). Replace with file contents if your build relies on them.
- Per-arch lockfiles remain Python-only.
- Windows host: the `--user` flag is skipped; Docker Desktop's default user mapping applies.

## v0.1.0 - 2026-05-27

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
