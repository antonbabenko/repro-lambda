import hashlib
import zipfile
from pathlib import Path

import pytest

from repro_lambda.zip_packager import pack_directory


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture
def pkg_dir(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "app.py").write_text("def lambda_handler(e, c): return 'ok'\n")
    (pkg / "data.json").write_text('{"k": "v"}\n')
    sub = pkg / "lib"
    sub.mkdir()
    (sub / "util.py").write_text("def f(): pass\n")
    exec_file = pkg / "tool.sh"
    exec_file.write_text("#!/bin/sh\necho hi\n")
    exec_file.chmod(0o755)
    return pkg


def test_pack_directory_produces_byte_identical_zip_when_called_twice(
    pkg_dir: Path, tmp_path: Path
):
    out1 = tmp_path / "lambda1.zip"
    out2 = tmp_path / "lambda2.zip"
    pack_directory(pkg_dir, out1)
    pack_directory(pkg_dir, out2)
    assert _sha256(out1) == _sha256(out2)


def test_pack_directory_entries_are_sorted(pkg_dir: Path, tmp_path: Path):
    out = tmp_path / "lambda.zip"
    pack_directory(pkg_dir, out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert names == sorted(names)


def test_pack_directory_mtime_is_1980_01_01(pkg_dir: Path, tmp_path: Path):
    out = tmp_path / "lambda.zip"
    pack_directory(pkg_dir, out)
    with zipfile.ZipFile(out) as zf:
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0), f"{info.filename} has bad mtime"


def test_pack_directory_preserves_executable_bit(pkg_dir: Path, tmp_path: Path):
    out = tmp_path / "lambda.zip"
    pack_directory(pkg_dir, out)
    with zipfile.ZipFile(out) as zf:
        for info in zf.infolist():
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.filename == "tool.sh":
                assert mode & 0o111, "executable bit lost on tool.sh"
            elif info.is_dir():
                assert (mode & 0o755) == 0o755, f"dir {info.filename} mode wrong"
            else:
                assert (mode & 0o644) == 0o644, f"file {info.filename} mode wrong"


def test_pack_directory_excludes_no_extras(pkg_dir: Path, tmp_path: Path):
    (pkg_dir / ".DS_Store").write_text("junk")
    out = tmp_path / "lambda.zip"
    pack_directory(pkg_dir, out, exclude_glob=[".DS_Store", "**/.DS_Store"])
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert ".DS_Store" not in names
