from pathlib import Path
from zipfile import ZipFile

from typer.testing import CliRunner

from repro_lambda.cli import app

runner = CliRunner()


def test_zip_excludes_caches_and_dist_info_metadata(tmp_path: Path):
    """`repro-lambda zip` strips caches + non-deterministic dist-info metadata,
    so the container build needs no find/xargs (absent from minimal base images)."""
    pkg = tmp_path / "pkg"
    (pkg / "mymod").mkdir(parents=True)
    (pkg / "mymod" / "__init__.py").write_text("x = 1\n")
    (pkg / "mymod" / "__pycache__").mkdir()
    (pkg / "mymod" / "__pycache__" / "__init__.cpython-313.pyc").write_bytes(b"\x00")
    (pkg / "mymod" / "stale.pyc").write_bytes(b"\x00")
    dist = pkg / "req-1.0.dist-info"
    dist.mkdir()
    (dist / "RECORD").write_text("mymod/__init__.py,,\n")
    (dist / "INSTALLER").write_text("pip\n")
    (dist / "METADATA").write_text("Name: req\n")

    out = tmp_path / "lambda.zip"
    result = runner.invoke(app, ["zip", "--src", str(pkg), "--out", str(out)])
    assert result.exit_code == 0, result.stdout

    names = ZipFile(out).namelist()
    # kept: real code + stable dist-info metadata
    assert "mymod/__init__.py" in names
    assert "req-1.0.dist-info/METADATA" in names
    # stripped: caches + non-deterministic metadata
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)
    assert "req-1.0.dist-info/RECORD" not in names
    assert "req-1.0.dist-info/INSTALLER" not in names
