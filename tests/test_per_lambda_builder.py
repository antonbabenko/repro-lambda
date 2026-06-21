"""Per-lambda builder overrides (base image / include / exclude), REPLACE-once-set."""

import subprocess
from pathlib import Path

import pytest

from repro_lambda.build import compute_sha_for
from repro_lambda.hasher import compute_content_hash
from repro_lambda.manifest import (
    BuilderConfig,
    LambdaSpec,
    load_manifest,
    resolve_builder,
)

PINNED_IMAGE = "public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64
OTHER_IMAGE = "public.ecr.aws/lambda/python:3.13@sha256:" + "1" * 64


def _spec(**kw) -> LambdaSpec:
    base = dict(
        logical_name="app",
        source_dir="handler",
        requirements_lock="handler/requirements.${arch}.lock",
        runtime="python3.13",
        arch="arm64",
        handler="app.lambda_handler",
    )
    base.update(kw)
    return LambdaSpec(**base)


def _default_builder() -> BuilderConfig:
    return BuilderConfig(
        base_image_python=PINNED_IMAGE,
        include_patterns=["**/*.py", "**/*.json"],
        exclude_patterns=["tests/**"],
    )


# --- manifest parse / validate --------------------------------------------


def _write_manifest(tmp_path: Path, lambda_overrides_toml: str) -> Path:
    p = tmp_path / "lambdas.toml"
    p.write_text(
        "[[lambda]]\n"
        'logical_name      = "app"\n'
        'source_dir        = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime           = "python3.13"\n'
        'arch              = "arm64"\n'
        'handler           = "app.lambda_handler"\n'
        f"{lambda_overrides_toml}"
        "\n[builder]\n"
        f'base_image_python = "{PINNED_IMAGE}"\n'
    )
    return p


def test_manifest_parses_per_lambda_builder_overrides(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path,
        f'base_image_python = "{OTHER_IMAGE}"\n'
        'include_patterns  = ["**/*.py"]\n'
        'exclude_patterns  = ["tests/**", "docs/**"]\n',
    )
    spec = load_manifest(manifest).lambdas[0]
    assert spec.base_image_python == OTHER_IMAGE
    assert spec.include_patterns == ["**/*.py"]
    assert spec.exclude_patterns == ["tests/**", "docs/**"]


def test_manifest_overrides_default_to_none(tmp_path: Path):
    spec = load_manifest(_write_manifest(tmp_path, "")).lambdas[0]
    assert spec.base_image_python is None
    assert spec.include_patterns is None
    assert spec.exclude_patterns is None


def test_manifest_explicit_empty_include_is_empty_list_not_none(tmp_path: Path):
    spec = load_manifest(_write_manifest(tmp_path, "include_patterns = []\n")).lambdas[0]
    assert spec.include_patterns == []  # explicit empty != unset


def test_manifest_rejects_unpinned_override_base_image(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path, 'base_image_python = "public.ecr.aws/lambda/python:3.13"\n'
    )
    with pytest.raises(ValueError, match="must be pinned by digest"):
        load_manifest(manifest)


def test_manifest_rejects_non_list_include(tmp_path: Path):
    manifest = _write_manifest(tmp_path, 'include_patterns = "**/*.py"\n')
    with pytest.raises(ValueError, match="must be a list of strings"):
        load_manifest(manifest)


def test_manifest_rejects_non_string_in_exclude(tmp_path: Path):
    manifest = _write_manifest(tmp_path, "exclude_patterns = [1, 2]\n")
    with pytest.raises(ValueError, match="must be a list of strings"):
        load_manifest(manifest)


# --- resolve_builder (REPLACE-once-set) ------------------------------------


def test_resolve_inherits_default_when_unset():
    default = _default_builder()
    resolved = resolve_builder(default, _spec())
    assert resolved.base_image_python == PINNED_IMAGE
    assert resolved.include_patterns == ["**/*.py", "**/*.json"]
    assert resolved.exclude_patterns == ["tests/**"]


def test_resolve_replaces_each_set_field():
    default = _default_builder()
    spec = _spec(
        base_image_python=OTHER_IMAGE,
        include_patterns=["src/**/*.py"],
        exclude_patterns=["docs/**"],
    )
    resolved = resolve_builder(default, spec)
    assert resolved.base_image_python == OTHER_IMAGE
    assert resolved.include_patterns == ["src/**/*.py"]
    assert resolved.exclude_patterns == ["docs/**"]


def test_resolve_empty_list_replaces_with_empty():
    default = _default_builder()
    resolved = resolve_builder(default, _spec(exclude_patterns=[]))
    assert resolved.exclude_patterns == []  # replace, not inherit


def test_resolve_preserves_default_nodejs_image():
    default = BuilderConfig(base_image_python=PINNED_IMAGE, base_image_nodejs="node@sha256:abc")
    resolved = resolve_builder(default, _spec(base_image_python=OTHER_IMAGE))
    assert resolved.base_image_nodejs == "node@sha256:abc"


# --- hash folds resolved include/exclude -----------------------------------


def _staged_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source"
    root.mkdir(parents=True)
    (root / "app.py").write_text("x = 1\n")
    lock = tmp_path / "requirements.lock"
    lock.write_text("")
    return root, lock


def _hash(root: Path, lock: Path, **kw) -> str:
    return compute_content_hash(
        staged_source_root=root,
        requirements_lock=lock,
        spec=_spec(),
        base_image=PINNED_IMAGE,
        builder_version="0.4.2",
        **kw,
    )


def test_hash_none_filters_is_stable(tmp_path: Path):
    root, lock = _staged_tree(tmp_path)
    assert _hash(root, lock) == _hash(root, lock, include_patterns=None, exclude_patterns=None)


def test_hash_rekeys_when_exclude_differs(tmp_path: Path):
    root, lock = _staged_tree(tmp_path)
    a = _hash(root, lock, include_patterns=["**/*.py"], exclude_patterns=["tests/**"])
    b = _hash(root, lock, include_patterns=["**/*.py"], exclude_patterns=["tests/**", "docs/**"])
    assert a != b


def test_hash_none_differs_from_explicit_empty(tmp_path: Path):
    root, lock = _staged_tree(tmp_path)
    unset = _hash(root, lock, include_patterns=None)
    empty = _hash(root, lock, include_patterns=[])
    assert unset != empty  # unset omits the section; [] emits an empty section marker


def test_hash_pattern_reorder_does_not_rekey(tmp_path: Path):
    root, lock = _staged_tree(tmp_path)
    a = _hash(root, lock, exclude_patterns=["a/**", "b/**"])
    b = _hash(root, lock, exclude_patterns=["b/**", "a/**"])
    assert a == b  # membership is order-independent -> sorted fold


# --- compute_sha_for integration (git-backed) ------------------------------


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "handler" / "tests").mkdir(parents=True)
    (repo / "handler" / "app.py").write_text("def lambda_handler(e, c): return 200\n")
    (repo / "handler" / "tests" / "test_app.py").write_text("def test_x(): pass\n")
    (repo / "handler" / "requirements.arm64.lock").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_compute_sha_for_rekeys_on_per_lambda_exclude(tmp_path: Path):
    repo = _git_repo(tmp_path)
    default = BuilderConfig(
        base_image_python=PINNED_IMAGE,
        include_patterns=["**/*.py"],
        exclude_patterns=[],
    )
    # Lambda A keeps tests; lambda B excludes them -> different staged tree -> different sha.
    spec_keep = _spec()
    spec_drop = _spec(exclude_patterns=["**/tests/**"])
    sha_keep = compute_sha_for(repo_root=repo, spec=spec_keep, builder=default)
    sha_drop = compute_sha_for(repo_root=repo, spec=spec_drop, builder=default)
    assert sha_keep != sha_drop


def test_compute_sha_for_rekeys_on_per_lambda_base_image(tmp_path: Path):
    repo = _git_repo(tmp_path)
    default = BuilderConfig(base_image_python=PINNED_IMAGE, include_patterns=["**/*.py"])
    sha_default = compute_sha_for(repo_root=repo, spec=_spec(), builder=default)
    sha_override = compute_sha_for(
        repo_root=repo, spec=_spec(base_image_python=OTHER_IMAGE), builder=default
    )
    assert sha_default != sha_override
