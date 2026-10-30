"""compile_bytecode: checked-hash .pyc built in the zip step, keyed into the content hash."""

import dataclasses
import importlib.util
import marshal
from pathlib import Path
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from repro_lambda.cli import app
from repro_lambda.hasher import compute_content_hash
from repro_lambda.manifest import LambdaSpec, load_manifest

runner = CliRunner()


def _pkg(root: Path) -> Path:
    pkg = root / "pkg"
    (pkg / "mymod" / "__pycache__").mkdir(parents=True)
    (pkg / "mymod" / "__init__.py").write_text("VALUES = frozenset({'a', 'b', 'c'})\n")
    (pkg / "mymod" / "__pycache__" / "stale.cpython-313.pyc").write_bytes(b"\x00")
    (pkg / "mymod" / "orphan.pyc").write_bytes(b"\x00")
    (pkg / "broken.py").write_text("def broken(:\n")
    return pkg


def _zip(pkg: Path, out: Path, *extra: str) -> list[str]:
    result = runner.invoke(app, ["zip", "--src", str(pkg), "--out", str(out), *extra])
    assert result.exit_code == 0, result.output
    return ZipFile(out).namelist()


def test_zip_compiles_checked_hash_bytecode_with_deployed_paths(tmp_path: Path):
    names = _zip(_pkg(tmp_path), tmp_path / "lambda.zip", "--compile-bytecode")
    pycs = [n for n in names if n.endswith(".pyc")]
    assert len(pycs) == 1 and pycs[0].startswith("mymod/__pycache__/__init__.")
    data = ZipFile(tmp_path / "lambda.zip").read(pycs[0])
    assert data[:4] == importlib.util.MAGIC_NUMBER
    assert int.from_bytes(data[4:8], "little") == 0b11  # hash-based, checked
    assert marshal.loads(data[16:]).co_filename == "/var/task/mymod/__init__.py"
    assert "mymod/orphan.pyc" not in names  # stale bytecode is replaced, not shipped
    assert "broken.py" in names  # a file that does not compile ships as source only


def test_compiled_zip_is_byte_identical_across_builds(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"  # different staging dirs, same tree
    _zip(_pkg(a), a / "lambda.zip", "--compile-bytecode")
    _zip(_pkg(b), b / "lambda.zip", "--compile-bytecode")
    assert (a / "lambda.zip").read_bytes() == (b / "lambda.zip").read_bytes()


def test_zip_without_the_flag_still_strips_bytecode(tmp_path: Path):
    names = _zip(_pkg(tmp_path), tmp_path / "lambda.zip")
    assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names)


def _spec(**kw) -> LambdaSpec:
    base = LambdaSpec(
        logical_name="app",
        source_dir="handler",
        requirements_lock="handler/requirements.${arch}.lock",
        runtime="python3.13",
        arch="arm64",
        handler="app.lambda_handler",
    )
    return dataclasses.replace(base, **kw)


def test_flag_changes_the_content_hash_and_default_keeps_it(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n")
    lock = tmp_path / "requirements.arm64.lock"
    lock.write_text("# empty\n")
    off = compute_content_hash(src, lock, _spec(), "x@sha256:0", "1")
    on = compute_content_hash(src, lock, _spec(compile_bytecode=True), "x@sha256:0", "1")
    assert off != on
    assert off == compute_content_hash(src, lock, _spec(compile_bytecode=False), "x@sha256:0", "1")


def _manifest(tmp_path: Path, extra: str, package_manager: str = "pip") -> Path:
    path = tmp_path / "lambdas.toml"
    path.write_text(
        f"""
[[lambda]]
logical_name = "app"
source_dir = "handler"
requirements_lock = "handler/requirements.${{arch}}.lock"
runtime = "python3.13"
arch = "arm64"
handler = "app.lambda_handler"
package_manager = "{package_manager}"
{extra}

[builder]
base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:abc"
"""
    )
    return path


def test_manifest_reads_the_flag(tmp_path: Path):
    assert (
        load_manifest(_manifest(tmp_path, "compile_bytecode = true")).lambdas[0].compile_bytecode
        is True
    )
    assert load_manifest(_manifest(tmp_path, "")).lambdas[0].compile_bytecode is False


def test_manifest_rejects_a_non_boolean_flag(tmp_path: Path):
    with pytest.raises(ValueError, match="compile_bytecode must be a boolean"):
        load_manifest(_manifest(tmp_path, 'compile_bytecode = "yes"'))
