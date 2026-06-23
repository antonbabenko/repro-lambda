"""Parse and validate lambdas.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_RUNTIMES = {
    "python3.11",
    "python3.12",
    "python3.13",
    "python3.14",
    "nodejs20.x",
    "nodejs22.x",
}
SUPPORTED_ARCHS: tuple[str, ...] = ("arm64", "x86_64")
SUPPORTED_PACKAGE_MANAGERS = {"pip", "npm"}


@dataclass(frozen=True)
class ExtraFile:
    """A prebuilt file or directory staged into the package alongside the source.

    `src` is relative to the repo root (where the caller's CI materialized it, e.g.
    a downloaded + digest-pinned binary or an extracted release tree). `dest` is
    where it lands in the package (relative to the package root). For a file,
    `executable` sets the +x bit; for a directory, source perms are preserved and
    `executable` is ignored. The bytes fold into the content hash via the staged
    source tree; the executable flag folds in separately, so flipping it changes
    the artifact hash even when bytes are unchanged.
    """

    src: str
    dest: str
    executable: bool = False


SUPPORTED_SOURCE_TYPES = {"github_release", "https"}
SUPPORTED_EXTRACT = {"zip", "tar.gz", "none"}


@dataclass(frozen=True)
class VersionFrom:
    """Lock-time version resolution rule for a source (never hashed).

    At `lock` time the referenced source (`source`, by name) is fetched + extracted,
    the asdf-style `<key> <value>` line is read from `file` (relative to that source's
    extracted tree), and the value re-pins this source's `version`. `build` never
    resolves this - it substitutes the already-locked `version` into the url/tag/asset
    templates. Single-level only: the referenced source may not itself use version_from.
    """

    source: str
    file: str
    key: str


@dataclass(frozen=True)
class Source:
    """A pinned external artifact fetched into the package before the container build.

    Two types: `github_release` (private, asset resolved via the GitHub API then
    downloaded) and `https` (public direct URL). Every source is PINNED: `sha256` is
    verified before extraction, and the resolved metadata (not the bytes) is what folds
    into the content hash. `extract` selects the archive handling (`zip`/`tar.gz`/`none`);
    `member`, when set, extracts a single archive entry to `dest`, otherwise the whole
    archive lands under `dest` (package-root-relative). `version`, when a `version_from`
    rule is present, is the lock-written concrete version substituted into the url/tag/
    asset templates' `{version}` placeholder at fetch + hash time.
    """

    name: str
    type: str
    sha256: str
    extract: str
    dest: str
    repo: str = ""
    tag: str = ""
    asset: str = ""
    url: str = ""
    member: str | None = None
    executable: bool = False
    version: str = ""
    version_from: VersionFrom | None = None

    def _subst(self, value: str) -> str:
        return value.replace("{version}", self.version) if self.version else value

    @property
    def resolved_url(self) -> str:
        return self._subst(self.url)

    @property
    def resolved_tag(self) -> str:
        return self._subst(self.tag)

    @property
    def resolved_asset(self) -> str:
        return self._subst(self.asset)

    @property
    def resolved_member(self) -> str | None:
        return self._subst(self.member) if self.member else self.member


@dataclass(frozen=True)
class LambdaSpec:
    logical_name: str
    source_dir: str
    requirements_lock: str  # template with ${arch} placeholder
    runtime: str
    arch: str
    handler: str
    region: str = "eu-west-1"
    package_manager: str = "pip"
    lambda_at_edge: bool = False
    hash_extra: str = ""
    package_json: str = ""
    extra_files: tuple[ExtraFile, ...] = ()
    # Per-lambda builder overrides (REPLACE-once-set; None = inherit the [builder]
    # default). base_image_python must stay digest-pinned. Resolved via resolve_builder().
    base_image_python: str | None = None
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    sources: tuple[Source, ...] = ()

    @property
    def resolved_requirements_lock(self) -> str:
        return self.requirements_lock.replace("${arch}", self.arch)

    @property
    def package_json_resolved(self) -> str:
        return self.package_json.replace("${arch}", self.arch)


@dataclass(frozen=True)
class BuilderConfig:
    base_image_python: str
    base_image_nodejs: str = ""
    include_patterns: list[str] = field(default_factory=lambda: ["**/*.py", "**/*.json"])
    exclude_patterns: list[str] = field(
        default_factory=lambda: [
            ".venv/**",
            ".pytest_cache/**",
            "__pycache__/**",
            "*.pyc",
            ".git/**",
            ".env*",
        ]
    )


@dataclass(frozen=True)
class Manifest:
    lambdas: list[LambdaSpec]
    builder: BuilderConfig


def resolve_builder(default: BuilderConfig, spec: LambdaSpec) -> BuilderConfig:
    """Resolve the effective builder for one lambda (REPLACE-once-set overrides).

    Each per-lambda override fully REPLACES the matching [builder] default when set;
    an unset override (None) inherits the default. An explicitly empty list replaces
    with empty (stages/filters nothing) - the caller's choice, distinct from unset.
    base_image_nodejs has no per-lambda override yet (add when a node lambda needs it).
    """
    return BuilderConfig(
        base_image_python=spec.base_image_python or default.base_image_python,
        base_image_nodejs=default.base_image_nodejs,
        include_patterns=(
            default.include_patterns if spec.include_patterns is None else spec.include_patterns
        ),
        exclude_patterns=(
            default.exclude_patterns if spec.exclude_patterns is None else spec.exclude_patterns
        ),
    )


def _parse_extra_files(path: Path, entry: dict) -> tuple[ExtraFile, ...]:
    """Parse + validate a lambda's optional [[lambda.extra_files]] entries."""
    parsed: list[ExtraFile] = []
    for ef in entry.get("extra_files", []):
        src = ef.get("src", "")
        dest = ef.get("dest", "")
        if not src or not dest:
            raise ValueError(
                f"{path}: extra_files entry requires non-empty 'src' and 'dest' (got {ef!r})"
            )
        for field_name, value in (("src", src), ("dest", dest)):
            if value.startswith("/") or ".." in Path(value).parts:
                raise ValueError(
                    f"{path}: extra_files {field_name}={value!r} must be a relative path "
                    f"without '..' (src is repo-root-relative, dest is package-root-relative)"
                )
        parsed.append(ExtraFile(src=src, dest=dest, executable=bool(ef.get("executable", False))))
    return tuple(parsed)


def _validate_relpath(path: Path, field: str, value: str, *, where: str) -> None:
    if value.startswith("/") or ".." in Path(value).parts:
        raise ValueError(f"{path}: {where} {field}={value!r} must be a relative path without '..'")


def _is_hex64(s: str) -> bool:
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def _paths_overlap(a: str, b: str) -> bool:
    """True if two normalized package-relative paths are equal or one is a tree prefix
    of the other (``a`` vs ``a/b``). Siblings (``a/b`` vs ``a/c``) do not overlap."""
    if a == b:
        return True
    pa, pb = a.split("/"), b.split("/")
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


def _reject_dest_overlaps(path: Path, named_dests: list[tuple[str, str]]) -> None:
    norm = [(name, Path(d).as_posix().strip("/")) for name, d in named_dests]
    for i in range(len(norm)):
        for j in range(i + 1, len(norm)):
            a_name, a = norm[i]
            b_name, b = norm[j]
            if _paths_overlap(a, b):
                raise ValueError(
                    f"{path}: source dest overlap between {a_name!r} ({a!r}) and "
                    f"{b_name!r} ({b!r}); each source must stage to a disjoint subtree"
                )


def _parse_sources(path: Path, entry: dict) -> tuple[Source, ...]:
    """Parse + validate a lambda's optional [[lambda.source]] entries."""
    parsed: list[Source] = []
    seen_names: set[str] = set()
    lname = entry.get("logical_name")
    for s in entry.get("source", []):
        name = s.get("name", "")
        if not name:
            raise ValueError(f"{path}: each [[lambda.source]] requires a non-empty 'name'")
        if name in seen_names:
            raise ValueError(f"{path}: duplicate source name {name!r} in lambda {lname!r}")
        seen_names.add(name)

        stype = s.get("type", "")
        if stype not in SUPPORTED_SOURCE_TYPES:
            raise ValueError(
                f"{path}: source {name!r} type must be one of "
                f"{sorted(SUPPORTED_SOURCE_TYPES)} (got {stype!r})"
            )

        extract = s.get("extract", "")
        if extract not in SUPPORTED_EXTRACT:
            raise ValueError(
                f"{path}: source {name!r} extract must be one of "
                f"{sorted(SUPPORTED_EXTRACT)} (got {extract!r})"
            )

        dest = s.get("dest", "")
        if not dest:
            raise ValueError(f"{path}: source {name!r} requires a non-empty 'dest'")
        _validate_relpath(path, "dest", dest, where=f"source {name!r}")

        sha256 = s.get("sha256", "")
        if sha256 and not _is_hex64(sha256):
            raise ValueError(
                f"{path}: source {name!r} sha256 must be 64 lowercase hex chars (got {sha256!r})"
            )

        member = s.get("member")
        if member is not None:
            if extract == "none":
                raise ValueError(
                    f"{path}: source {name!r} sets 'member' but extract='none' "
                    f"(no archive to extract a member from)"
                )
            _validate_relpath(path, "member", member, where=f"source {name!r}")

        repo = s.get("repo", "")
        tag = s.get("tag", "")
        asset = s.get("asset", "")
        url = s.get("url", "")
        if stype == "github_release":
            for fn, fv in (("repo", repo), ("tag", tag), ("asset", asset)):
                if not fv:
                    raise ValueError(
                        f"{path}: github_release source {name!r} requires non-empty {fn!r}"
                    )
            if repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
                raise ValueError(
                    f"{path}: github_release source {name!r} repo must be 'owner/name' "
                    f"(got {repo!r})"
                )
            if url:
                raise ValueError(
                    f"{path}: github_release source {name!r} must not set 'url' "
                    f"(it is resolved from repo/tag/asset)"
                )
        else:  # https
            if not url:
                raise ValueError(f"{path}: https source {name!r} requires non-empty 'url'")
            if not url.startswith("https://"):
                raise ValueError(
                    f"{path}: https source {name!r} url must start with https:// (got {url!r})"
                )
            for fn, fv in (("repo", repo), ("tag", tag), ("asset", asset)):
                if fv:
                    raise ValueError(
                        f"{path}: https source {name!r} must not set {fn!r} (use 'url')"
                    )

        version = s.get("version", "")
        vf_raw = s.get("version_from")
        version_from = None
        if vf_raw is not None:
            for fn in ("source", "file", "key"):
                if not vf_raw.get(fn):
                    raise ValueError(
                        f"{path}: source {name!r} version_from requires non-empty {fn!r}"
                    )
            if vf_raw["source"] == name:
                raise ValueError(
                    f"{path}: source {name!r} version_from.source cannot reference itself"
                )
            _validate_relpath(path, "version_from.file", vf_raw["file"], where=f"source {name!r}")
            version_from = VersionFrom(
                source=vf_raw["source"], file=vf_raw["file"], key=vf_raw["key"]
            )

        uses_template = any("{version}" in v for v in (url, tag, asset, member or ""))
        if uses_template and not version and version_from is None:
            raise ValueError(
                f"{path}: source {name!r} uses '{{version}}' but has no 'version' value or "
                f"[lambda.source.version_from] rule to resolve it"
            )

        parsed.append(
            Source(
                name=name,
                type=stype,
                sha256=sha256,
                extract=extract,
                dest=dest,
                repo=repo,
                tag=tag,
                asset=asset,
                url=url,
                member=member,
                executable=bool(s.get("executable", False)),
                version=version,
                version_from=version_from,
            )
        )

    by_name = {src.name: src for src in parsed}
    for src in parsed:
        if src.version_from is None:
            continue
        ref = src.version_from.source
        if ref not in by_name:
            raise ValueError(
                f"{path}: source {src.name!r} version_from.source={ref!r} is not a defined source"
            )
        if by_name[ref].version_from is not None:
            raise ValueError(
                f"{path}: source {src.name!r} version_from.source={ref!r} is itself "
                f"version_from-resolved (single-level only)"
            )
        if by_name[ref].extract == "none":
            raise ValueError(
                f"{path}: source {src.name!r} version_from.source={ref!r} has extract='none'; "
                f"a referenced source must be an archive to read its version file"
            )

    _reject_dest_overlaps(path, [(src.name, src.dest) for src in parsed])
    return tuple(parsed)


def load_manifest(path: Path) -> Manifest:
    """Parse lambdas.toml and validate semantic invariants."""
    with path.open("rb") as f:
        raw = tomllib.load(f)

    if "lambda" not in raw or not raw["lambda"]:
        raise ValueError(f"{path}: must define at least one [[lambda]] entry")
    if "builder" not in raw:
        raise ValueError(f"{path}: missing [builder] section")

    builder_raw = raw["builder"]
    base_image_python = builder_raw.get("base_image_python", "")
    if "@sha256:" not in base_image_python:
        raise ValueError(
            f"{path}: builder.base_image_python must be pinned by digest "
            f"(got {base_image_python!r}; need image@sha256:<digest>)"
        )
    base_image_nodejs = builder_raw.get("base_image_nodejs", "")

    npm_used = any(entry.get("package_manager") == "npm" for entry in raw["lambda"])
    if npm_used and "@sha256:" not in base_image_nodejs:
        raise ValueError(
            f"{path}: builder.base_image_nodejs must be pinned by digest when any lambda uses npm "
            f"(got {base_image_nodejs!r}; need image@sha256:<digest>)"
        )

    defaults = BuilderConfig(base_image_python=base_image_python)
    builder = BuilderConfig(
        base_image_python=base_image_python,
        base_image_nodejs=base_image_nodejs,
        include_patterns=list(builder_raw.get("include_patterns", defaults.include_patterns)),
        exclude_patterns=list(builder_raw.get("exclude_patterns", defaults.exclude_patterns)),
    )

    lambdas: list[LambdaSpec] = []
    for entry in raw["lambda"]:
        runtime = entry.get("runtime")
        if runtime not in SUPPORTED_RUNTIMES:
            raise ValueError(
                f"{path}: unsupported runtime {runtime!r}; supported: {sorted(SUPPORTED_RUNTIMES)}"
            )
        arch = entry.get("arch")
        if arch not in SUPPORTED_ARCHS:
            raise ValueError(
                f"{path}: unsupported arch {arch!r}; supported: {list(SUPPORTED_ARCHS)}"
            )
        pkg = entry.get("package_manager", "pip")
        if pkg not in SUPPORTED_PACKAGE_MANAGERS:
            raise ValueError(f"{path}: unsupported package_manager {pkg!r}")
        package_json = entry.get("package_json", "")
        if pkg == "npm" and not package_json:
            raise ValueError(
                f"{path}: npm specs require 'package_json' (got empty); "
                f"point it at the lambda's package.json relative to repo root"
            )

        extra_files = _parse_extra_files(path, entry)
        sources = _parse_sources(path, entry)

        override_base_image = entry.get("base_image_python")
        if override_base_image is not None and "@sha256:" not in override_base_image:
            raise ValueError(
                f"{path}: lambda {entry.get('logical_name')!r} base_image_python override must be "
                f"pinned by digest (got {override_base_image!r}; need image@sha256:<digest>)"
            )
        override_include = entry.get("include_patterns")
        override_exclude = entry.get("exclude_patterns")
        for fname, fval in (
            ("include_patterns", override_include),
            ("exclude_patterns", override_exclude),
        ):
            if fval is not None and (
                not isinstance(fval, list) or not all(isinstance(x, str) for x in fval)
            ):
                raise ValueError(
                    f"{path}: lambda {entry.get('logical_name')!r} {fname} override "
                    f"must be a list of strings (got {fval!r})"
                )

        lambdas.append(
            LambdaSpec(
                logical_name=entry["logical_name"],
                source_dir=entry["source_dir"],
                requirements_lock=entry["requirements_lock"],
                package_json=package_json,
                runtime=runtime,
                arch=arch,
                handler=entry["handler"],
                region=entry.get("region", "eu-west-1"),
                package_manager=pkg,
                lambda_at_edge=bool(entry.get("lambda_at_edge", False)),
                hash_extra=entry.get("hash_extra", ""),
                extra_files=extra_files,
                base_image_python=override_base_image,
                include_patterns=list(override_include) if override_include is not None else None,
                exclude_patterns=list(override_exclude) if override_exclude is not None else None,
                sources=sources,
            )
        )

    return Manifest(lambdas=lambdas, builder=builder)
