# Changelog

## v0.5.0 - 2026-06-21

### Added
- Declarative sources DSL: `[[lambda.source]]` fetches a pinned external artifact into the package before the container build, replacing consumer-side download scripts. Two source types - `github_release` (`repo`/`tag`/`asset`, resolved via the GitHub API) and `https` (a direct `url`). Each source is PINNED: `sha256` is verified before extraction; `extract` is `zip`/`tar.gz`/`none`; an optional `member` extracts a single file or a (versioned) directory subtree to `dest` (package-root-relative); `executable` sets +x. Source names are required and unique per lambda; dest collisions (including tree overlaps and overlaps with the staged source) are refused.
- `version_from` (single-level): a source can derive its `version` at lock time from an asdf-style `key value` line in a referenced source's file (read relative to that source's member-stripped tree). `{version}` is substituted into `url`/`tag`/`asset`/`member`. `version_from` is a lock input and never participates in the content hash.
- `lock --sources` re-resolves `version_from`, re-downloads each source, recomputes its sha256, and rewrites `lambdas.toml` in place with tomlkit (comments preserved, atomic). It is idempotent: a run that changes nothing leaves the file byte-for-byte unchanged (no spurious PR). `lock` keeps regenerating requirements locks too; use `--no-requirements` / `--no-sources` to scope it.
- The reusable `build.yml` gains a generic `sources_token` secret (exported as `REPRO_LAMBDA_SOURCES_TOKEN`, used only for `github_release` API calls) and an arch-scoped `actions/cache` for the content-addressed source cache.

### Security
- SSRF-hardened fetcher: HTTPS-only with a manual redirect loop; each hop resolves the hostname, refuses any non-global-unicast / reserved / private / loopback / link-local / IPv4-mapped address, and connects to that pinned IP while verifying the TLS cert against the original hostname (no second DNS lookup a rebind could poison). `Authorization` is stripped on any cross-host redirect (e.g. the GitHub API -> asset host), so a private token never leaks. Download size is capped and log URLs are redacted (userinfo + query stripped).
- Hardened extraction: sha256 is verified before any archive is opened; entries with absolute / `..` / symlink / hardlink / device / fifo paths are rejected; total bytes, entry count, and per-entry bytes are bounded (decompression-bomb limits, with streaming tar reads); files are written to a temp tree then promoted with normalized mtime + perms. The sha256-keyed cache is re-verified on every use (anti cache-poisoning).
- Content hashing folds the RESOLVED source metadata (not the bytes), so the artifact key is computable offline and a `member`/`extract`/`dest`/`sha256`/version change re-keys, while a re-fetch alone does not.

## v0.4.2 - 2026-06-21

### Added
- Per-lambda builder overrides: any `[[lambda]]` may now set `base_image_python`, `include_patterns`, or `exclude_patterns` to override the `[builder]` defaults for itself. An override fully REPLACES the matching default (lists are not merged); an unset field inherits `[builder]`. A per-lambda `base_image_python` must still be digest-pinned (validated at manifest load). This lets one lambda build on its own base image or filter its source more tightly than the others - e.g. a lambda that bundles a large prebuilt tree can narrow `include_patterns` to just its runtime modules so unrelated file changes no longer re-key its artifact. The resolved per-lambda builder (base-image digest + include/exclude lists + builder version) folds into the content hash, so changing an override re-keys only that lambda. Manifests with no per-lambda overrides resolve to the `[builder]` defaults unchanged. The builder version bump re-keys all content hashes, as expected.

## v0.4.1 - 2026-06-21

### Fixed
- Lower the pip `--platform` floor from `manylinux_2_28` to `manylinux_2_17` (manylinux2014) for both arches. pip's explicit `--platform` does not expand a higher manylinux tag down to lower-baseline wheels, so with `--only-binary=:all:` a `2_28` floor failed to find compiled wheels that ship only `manylinux_2_17` for a given Python/arch (e.g. `pydantic-core`). `manylinux_2_17` is the broadest baseline the AWS Lambda base images (Amazon Linux 2023, glibc 2.34) still run, and it matches `2_17` wheels plus any lower baseline. Pure-Python lambdas are unaffected (their `py3-none-any` wheels never depended on the platform). The builder version bump re-keys all content hashes, as expected.

## v0.4.0 - 2026-06-21

### Added
- `extra_files` manifest field: bundle prebuilt files or directories into a lambda package alongside its source. Each `[[lambda.extra_files]]` entry has `src` (repo-root-relative, where CI materialized it - e.g. a digest-pinned binary or an extracted release tree), `dest` (package-root-relative), and an optional `executable` flag (sets +x on a file; ignored for directories, which keep source perms). The bytes fold into the content hash via the staged source tree, and the executable bit folds in separately, so flipping it changes the artifact hash even when bytes are identical. This lets a lambda ship vendored CLIs or release trees the consumer's CI downloads and verifies, while the tool itself stays free of any network/tool-download logic. Paths are validated as relative and `..`-free. Specs without `extra_files` hash byte-identically to before.

## v0.3.0 - 2026-06-21

### Added
- `promote` command: copy an already-built artifact from the dev bucket to the prod bucket by content hash, with no rebuild. The sha per lambda is read from `builds/catalog.json`, so the promoted object is byte-for-byte the one built and tested in dev. Idempotent (skips keys already present); Lambda@Edge specs resolve to the `-us-east-1` bucket variant on both sides. `S3Uploader.copy()` performs the server-side `CopyObject`.
- Reusable workflow `promote.yml`: validates `source_sha` (40-char hex + master-lineage), checks it out, assumes a caller-supplied promoter role, and runs `promote`. Inputs: `manifest_path`, `source_sha`, `promoter_role_arn`, `dev_bucket`, `prod_bucket`.

### Consumer migration
- Replace a rebuild-on-prod `promote-to-prod` job with a call to the reusable workflow:

      jobs:
        promote:
          uses: antonbabenko/repro-lambda/.github/workflows/promote.yml@v0
          with:
            source_sha: ${{ inputs.source_sha }}
            promoter_role_arn: arn:aws:iam::<account>:role/<promoter-role>
            dev_bucket: <env>-my-lambda-artifacts
            prod_bucket: <env>-my-lambda-artifacts

## v0.2.4 - 2026-06-20

### Fixed
- Container build no longer shells out to `find`/`xargs` (both absent from the minimal AWS Lambda base images, which caused `find: command not found`). The post-install cleanup (Python caches + non-deterministic `*.dist-info` metadata: RECORD, INSTALLER, direct_url.json, REQUESTED) now happens in the Python zip step via exclude globs, producing the same artifact bytes.

## v0.2.3 - 2026-06-20

### Fixed
- Python build container staged dependencies under `/build` (root-owned), which failed with `mkdir: Permission denied` when the container runs as a non-root `--user` (e.g. GitHub-hosted runners, uid 1001). Staging moved to `/tmp/build` (world-writable).

### Added
- `build --arch <arm64|x86_64>` filters the manifest to lambdas of that arch, so a per-arch CI matrix builds each arch natively on its own runner. This avoids cross-arch `docker run` (which fails without emulation, and emulated builds would break byte-reproducibility). The reusable `build.yml` passes `--arch ${{ matrix.arch }}`.

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
