"""compute_content_hash folds RESOLVED source metadata, download-free + deterministic."""

from pathlib import Path

from repro_lambda.hasher import compute_content_hash
from repro_lambda.manifest import LambdaSpec, Source, VersionFrom

PINNED = "public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64


def _spec() -> LambdaSpec:
    return LambdaSpec(
        logical_name="app",
        source_dir="handler",
        requirements_lock="handler/requirements.${arch}.lock",
        runtime="python3.13",
        arch="arm64",
        handler="app.lambda_handler",
    )


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source"
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("x = 1\n")
    lock = tmp_path / "req.lock"
    lock.write_text("")
    return root, lock


def _hash(tmp_path: Path, sources) -> str:
    root, lock = _tree(tmp_path)
    return compute_content_hash(
        staged_source_root=root,
        requirements_lock=lock,
        spec=_spec(),
        base_image=PINNED,
        builder_version="0.5.0",
        sources=sources,
    )


def _src(**kw) -> Source:
    base = dict(
        name="tf", type="https", sha256="a" * 64, extract="zip", dest="bin/tf", url="https://e/a"
    )
    base.update(kw)
    return Source(**base)


def test_none_equals_empty_and_omits_section(tmp_path):
    assert _hash(tmp_path, None) == _hash(tmp_path, ())


def test_adding_a_source_rekeys(tmp_path):
    assert _hash(tmp_path, None) != _hash(tmp_path, (_src(),))


def test_member_change_rekeys(tmp_path):
    a = _hash(tmp_path, (_src(member="terraform"),))
    b = _hash(tmp_path, (_src(member="tofu"),))
    assert a != b


def test_extract_change_rekeys(tmp_path):
    assert _hash(tmp_path, (_src(extract="zip"),)) != _hash(tmp_path, (_src(extract="tar.gz"),))


def test_sha256_change_rekeys(tmp_path):
    assert _hash(tmp_path, (_src(sha256="a" * 64),)) != _hash(tmp_path, (_src(sha256="b" * 64),))


def test_dest_change_rekeys(tmp_path):
    assert _hash(tmp_path, (_src(dest="bin/a"),)) != _hash(tmp_path, (_src(dest="bin/b"),))


def test_executable_change_rekeys(tmp_path):
    assert _hash(tmp_path, (_src(executable=False),)) != _hash(tmp_path, (_src(executable=True),))


def test_order_independent(tmp_path):
    a = _src(name="a", dest="bin/a")
    b = _src(name="b", dest="bin/b")
    assert _hash(tmp_path, (a, b)) == _hash(tmp_path, (b, a))


def test_version_from_not_hashed(tmp_path):
    # Same resolved metadata; only the (lock-input) version_from rule differs -> same key.
    with_rule = _src(
        version="1.0",
        url="https://e/{version}",
        member="x",
        version_from=VersionFrom(source="pofix", file=".tool-versions", key="tf"),
    )
    without = _src(version="1.0", url="https://e/{version}", member="x", version_from=None)
    assert _hash(tmp_path, (with_rule,)) == _hash(tmp_path, (without,))


def test_resolved_version_substitution_is_what_hashes(tmp_path):
    # A {version} template + version == a concrete URL with no template.
    templated = _src(url="https://e/{version}/tf.zip", version="1.0")
    concrete = _src(url="https://e/1.0/tf.zip", version="")
    assert _hash(tmp_path, (templated,)) == _hash(tmp_path, (concrete,))


def test_different_version_rekeys(tmp_path):
    a = _src(url="https://e/{version}/tf.zip", version="1.0")
    b = _src(url="https://e/{version}/tf.zip", version="2.0")
    assert _hash(tmp_path, (a,)) != _hash(tmp_path, (b,))
