from pathlib import Path

from repro_lambda import __version__
from repro_lambda.hasher import compute_content_hash
from repro_lambda.manifest import LambdaSpec


def _spec(arch: str = "arm64", hash_extra: str = "") -> LambdaSpec:
    return LambdaSpec(
        logical_name="app",
        source_dir="handler",
        requirements_lock="handler/requirements.${arch}.lock",
        runtime="python3.13",
        arch=arch,
        handler="app.lambda_handler",
        region="eu-west-1",
        package_manager="pip",
        lambda_at_edge=False,
        hash_extra=hash_extra,
    )


def test_compute_content_hash_is_stable(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("def lambda_handler(e, c): return 'ok'\n")
    lock = tmp_path / "requirements.arm64.lock"
    lock.write_text("# empty\n")

    base_image = "public.ecr.aws/lambda/python:3.13@sha256:abc123"

    h1 = compute_content_hash(src, lock, _spec(), base_image, __version__)
    h2 = compute_content_hash(src, lock, _spec(), base_image, __version__)
    assert h1 == h2
    assert len(h1) == 64


def test_compute_content_hash_changes_on_source_change(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("def lambda_handler(e, c): return 'ok'\n")
    lock = tmp_path / "requirements.arm64.lock"
    lock.write_text("# empty\n")

    h1 = compute_content_hash(src, lock, _spec(), "x@sha256:0", "1")
    (src / "app.py").write_text("def lambda_handler(e, c): return 'changed'\n")
    h2 = compute_content_hash(src, lock, _spec(), "x@sha256:0", "1")
    assert h1 != h2


def test_compute_content_hash_changes_on_arch_change(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("ok\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("\n")
    h_arm = compute_content_hash(src, lock, _spec(arch="arm64"), "x@sha256:0", "1")
    h_x86 = compute_content_hash(src, lock, _spec(arch="x86_64"), "x@sha256:0", "1")
    assert h_arm != h_x86


def test_compute_content_hash_changes_on_base_image_change(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("ok\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("\n")
    h1 = compute_content_hash(src, lock, _spec(), "x@sha256:0", "1")
    h2 = compute_content_hash(src, lock, _spec(), "x@sha256:1", "1")
    assert h1 != h2


def test_compute_content_hash_changes_on_builder_version_change(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("ok\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("\n")
    h1 = compute_content_hash(src, lock, _spec(), "x@sha256:0", "1")
    h2 = compute_content_hash(src, lock, _spec(), "x@sha256:0", "2")
    assert h1 != h2


def test_compute_content_hash_changes_on_hash_extra_change(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("ok\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("\n")
    h1 = compute_content_hash(src, lock, _spec(hash_extra=""), "x@sha256:0", "1")
    h2 = compute_content_hash(src, lock, _spec(hash_extra="bust"), "x@sha256:0", "1")
    assert h1 != h2


def test_compute_content_hash_with_extras_is_host_path_independent(tmp_path: Path):
    """Same dest relname + same bytes => same hash, even from different host dirs."""
    staged = tmp_path / "src"
    staged.mkdir()
    (staged / "a.py").write_text("a\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("L\n")
    spec = LambdaSpec(
        logical_name="x",
        source_dir="x",
        requirements_lock="lock.txt",
        runtime="python3.13",
        arch="arm64",
        handler="x.h",
    )
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    (dir_a / "pj").write_text("{}")
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    (dir_b / "pj").write_text("{}")

    h1 = compute_content_hash(
        staged,
        lock,
        spec,
        "img",
        "1",
        extra_files=[(dir_a / "pj", "package.json")],
    )
    h2 = compute_content_hash(
        staged,
        lock,
        spec,
        "img",
        "1",
        extra_files=[(dir_b / "pj", "package.json")],
    )
    assert h1 == h2, "hash must depend on (relname, bytes), not host path"


def test_compute_content_hash_extras_collide_only_on_relname(tmp_path: Path):
    """Two extras with the same BASENAME but different relnames must produce different hashes."""
    staged = tmp_path / "src"
    staged.mkdir()
    (staged / "a.py").write_text("a\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("L\n")
    spec = LambdaSpec(
        logical_name="x",
        source_dir="x",
        requirements_lock="lock.txt",
        runtime="python3.13",
        arch="arm64",
        handler="x.h",
    )
    pj_a = tmp_path / "outer" / "package.json"
    pj_a.parent.mkdir()
    pj_a.write_text("{}")
    pj_b = tmp_path / "inner" / "package.json"
    pj_b.parent.mkdir()
    pj_b.write_text("{}")

    h_flat = compute_content_hash(
        staged,
        lock,
        spec,
        "img",
        "1",
        extra_files=[(pj_a, "package.json")],
    )
    h_nested = compute_content_hash(
        staged,
        lock,
        spec,
        "img",
        "1",
        extra_files=[(pj_b, "workspaces/api/package.json")],
    )
    assert h_flat != h_nested, "different relnames must hash differently even with same bytes"


def test_compute_content_hash_extras_change_when_content_changes(tmp_path: Path):
    staged = tmp_path / "src"
    staged.mkdir()
    (staged / "a.py").write_text("a\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("L\n")
    spec = LambdaSpec(
        logical_name="x",
        source_dir="x",
        requirements_lock="lock.txt",
        runtime="python3.13",
        arch="arm64",
        handler="x.h",
    )
    pj_v1 = tmp_path / "v1" / "package.json"
    pj_v1.parent.mkdir()
    pj_v1.write_text("{}")
    pj_v2 = tmp_path / "v2" / "package.json"
    pj_v2.parent.mkdir()
    pj_v2.write_text('{"a":1}')

    h1 = compute_content_hash(staged, lock, spec, "img", "1", extra_files=[(pj_v1, "package.json")])
    h2 = compute_content_hash(staged, lock, spec, "img", "1", extra_files=[(pj_v2, "package.json")])
    assert h1 != h2
