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
