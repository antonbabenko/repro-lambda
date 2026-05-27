"""Parse and validate lambdas.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_RUNTIMES = {
    "python3.11", "python3.12", "python3.13",
    "nodejs20.x", "nodejs22.x",
}
SUPPORTED_ARCHS: tuple[str, ...] = ("arm64", "x86_64")
SUPPORTED_PACKAGE_MANAGERS = {"pip", "npm"}


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
            )
        )

    return Manifest(lambdas=lambdas, builder=builder)
