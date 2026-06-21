"""extra_files: prebuilt file/dir bundling into the content-addressed package."""

import subprocess
from pathlib import Path

import pytest

from repro_lambda.build import compute_sha_for
from repro_lambda.hasher import compute_content_hash
from repro_lambda.manifest import BuilderConfig, ExtraFile, LambdaSpec, load_manifest
from repro_lambda.source_stager import stage_source

PINNED_IMAGE = "public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64


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


# --- manifest parse / validate --------------------------------------------


def _write_manifest(tmp_path: Path, extra_files_toml: str) -> Path:
    p = tmp_path / "lambdas.toml"
    p.write_text(
        "[[lambda]]\n"
        'logical_name      = "app"\n'
        'source_dir        = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime           = "python3.13"\n'
        'arch              = "arm64"\n'
        'handler           = "app.lambda_handler"\n'
        f"{extra_files_toml}"
        "\n[builder]\n"
        f'base_image_python = "{PINNED_IMAGE}"\n'
    )
    return p


def test_manifest_parses_extra_files(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path,
        '[[lambda.extra_files]]\nsrc = "builds/bin/terraform"\n'
        'dest = "bin/terraform"\nexecutable = true\n'
        '[[lambda.extra_files]]\nsrc = "builds/pofix"\ndest = "pofix"\n',
    )
    spec = load_manifest(manifest).lambdas[0]
    assert spec.extra_files == (
        ExtraFile(src="builds/bin/terraform", dest="bin/terraform", executable=True),
        ExtraFile(src="builds/pofix", dest="pofix", executable=False),
    )


def test_manifest_extra_files_defaults_empty(tmp_path: Path):
    manifest = _write_manifest(tmp_path, "")
    assert load_manifest(manifest).lambdas[0].extra_files == ()


def test_manifest_rejects_extra_files_without_dest(tmp_path: Path):
    manifest = _write_manifest(tmp_path, '[[lambda.extra_files]]\nsrc = "builds/bin/terraform"\n')
    with pytest.raises(ValueError, match="non-empty 'src' and 'dest'"):
        load_manifest(manifest)


def test_manifest_rejects_extra_files_parent_traversal(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path,
        '[[lambda.extra_files]]\nsrc = "builds/bin/x"\ndest = "../escape"\n',
    )
    with pytest.raises(ValueError, match="without '..'"):
        load_manifest(manifest)


def test_manifest_rejects_extra_files_absolute_src(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path,
        '[[lambda.extra_files]]\nsrc = "/etc/passwd"\ndest = "bin/x"\n',
    )
    with pytest.raises(ValueError, match="relative path"):
        load_manifest(manifest)


# --- stager ----------------------------------------------------------------


def _git_repo_with_source(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "handler").mkdir(parents=True)
    (repo / "handler" / "app.py").write_text("def lambda_handler(e, c): return 200\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_stage_payload_file_gets_exec_bit(tmp_path: Path):
    repo = _git_repo_with_source(tmp_path)
    (repo / "builds" / "bin").mkdir(parents=True)
    binary = repo / "builds" / "bin" / "terraform"
    binary.write_bytes(b"\x7fELF fake binary")

    stage = tmp_path / "stage"
    stage_source(
        repo_root=repo,
        source_dir="handler",
        builder=BuilderConfig(base_image_python=PINNED_IMAGE),
        stage_dir=stage,
        payload_files=[
            ExtraFile(src="builds/bin/terraform", dest="bin/terraform", executable=True)
        ],
    )
    staged = stage / "source" / "bin" / "terraform"
    assert staged.read_bytes() == b"\x7fELF fake binary"
    assert staged.stat().st_mode & 0o111, "executable bit not set"


def test_stage_payload_dir_recursive(tmp_path: Path):
    repo = _git_repo_with_source(tmp_path)
    tree = repo / "builds" / "pofix"
    (tree / "data").mkdir(parents=True)
    (tree / ".tool-versions").write_text("terraform 1.9.0\n")
    (tree / "data" / "x.json").write_text("{}\n")

    stage = tmp_path / "stage"
    stage_source(
        repo_root=repo,
        source_dir="handler",
        builder=BuilderConfig(base_image_python=PINNED_IMAGE),
        stage_dir=stage,
        payload_files=[ExtraFile(src="builds/pofix", dest="pofix")],
    )
    assert (stage / "source" / "pofix" / ".tool-versions").read_text() == "terraform 1.9.0\n"
    assert (stage / "source" / "pofix" / "data" / "x.json").exists()


def test_stage_payload_missing_src_raises(tmp_path: Path):
    repo = _git_repo_with_source(tmp_path)
    stage = tmp_path / "stage"
    with pytest.raises(FileNotFoundError, match="extra_files src not found"):
        stage_source(
            repo_root=repo,
            source_dir="handler",
            builder=BuilderConfig(base_image_python=PINNED_IMAGE),
            stage_dir=stage,
            payload_files=[ExtraFile(src="builds/missing", dest="bin/x")],
        )


# --- hasher ----------------------------------------------------------------


def _staged_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source"
    (root / "bin").mkdir(parents=True)
    (root / "app.py").write_text("x = 1\n")
    (root / "bin" / "terraform").write_bytes(b"BIN")
    lock = tmp_path / "requirements.lock"
    lock.write_text("")
    return root, lock


def _hash(root: Path, lock: Path, *, payload_exec=None) -> str:
    return compute_content_hash(
        staged_source_root=root,
        requirements_lock=lock,
        spec=_spec(),
        base_image=PINNED_IMAGE,
        builder_version="0.3.0",
        payload_exec=payload_exec,
    )


def test_hash_none_equals_empty_payload_exec(tmp_path: Path):
    root, lock = _staged_tree(tmp_path)
    assert _hash(root, lock, payload_exec=None) == _hash(root, lock, payload_exec=[])


def test_hash_changes_when_executable_flag_flips(tmp_path: Path):
    root, lock = _staged_tree(tmp_path)
    off = _hash(root, lock, payload_exec=[("bin/terraform", False)])
    on = _hash(root, lock, payload_exec=[("bin/terraform", True)])
    assert off != on


def test_hash_omits_payload_section_when_empty(tmp_path: Path):
    # Byte-identical to a spec with no extra_files at all (regression guard for
    # the 3 existing lambdas, whose hashes must not move).
    root, lock = _staged_tree(tmp_path)
    assert _hash(root, lock) == _hash(root, lock, payload_exec=[])


# --- build.compute_sha_for integration -------------------------------------


def test_compute_sha_for_reflects_payload_binary(tmp_path: Path):
    repo = _git_repo_with_source(tmp_path)
    (repo / "handler" / "requirements.arm64.lock").write_text("")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "lock"], cwd=repo, check=True)
    (repo / "builds" / "bin").mkdir(parents=True)
    binary = repo / "builds" / "bin" / "terraform"

    builder = BuilderConfig(base_image_python=PINNED_IMAGE)
    spec = _spec(extra_files=(ExtraFile(src="builds/bin/terraform", dest="bin/terraform"),))

    binary.write_bytes(b"VERSION-A")
    sha_a = compute_sha_for(repo_root=repo, spec=spec, builder=builder)
    binary.write_bytes(b"VERSION-B")
    sha_b = compute_sha_for(repo_root=repo, spec=spec, builder=builder)

    assert sha_a != sha_b, "content hash must track the bundled binary's bytes"
