"""lock_sources: version_from resolution, sha re-pin, idempotent tomlkit rewrite."""

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

from repro_lambda import source_locker
from repro_lambda.manifest import load_manifest
from repro_lambda.source_locker import lock_sources

OLD_SHA = "0" * 64


def _targz(path: Path, files: list[tuple[str, bytes]]) -> bytes:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path.read_bytes()


def _zip(path: Path, entries: list[tuple[str, bytes]]) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path.read_bytes()


def _manifest(tmp_path: Path) -> Path:
    p = tmp_path / "lambdas.toml"
    p.write_text(
        "# keep this comment\n"
        "[[lambda]]\n"
        'logical_name      = "pofix_lambda"\n'
        'source_dir        = "src/pofix"\n'
        'requirements_lock = "src/pofix/requirements.${arch}.lock"\n'
        'runtime           = "python3.13"\n'
        'arch              = "arm64"\n'
        'handler           = "handler.lambda_handler"\n'
        "\n"
        "[[lambda.source]]\n"
        'name    = "pofix"\n'
        'type    = "https"\n'
        'url     = "https://example.com/pofix-{version}.tar.gz"\n'
        f'sha256  = "{OLD_SHA}"\n'
        'extract = "tar.gz"\n'
        'member  = "pofix-{version}"\n'
        'dest    = "pofix"\n'
        'version = "9.9"\n'
        "\n"
        "[[lambda.source]]\n"
        'name    = "terraform"\n'
        'type    = "https"\n'
        'url     = "https://example.com/terraform_{version}_linux_arm64.zip"\n'
        f'sha256  = "{OLD_SHA}"\n'
        'extract = "zip"\n'
        'member  = "terraform"\n'
        'dest    = "bin/terraform"\n'
        'version = "0.0.0"\n'  # stale; lock derives it from pofix's .tool-versions
        "[lambda.source.version_from]\n"
        'source = "pofix"\n'
        'file   = ".tool-versions"\n'
        'key    = "terraform"\n'
        "\n[builder]\n"
        f'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:{"0" * 64}"\n'
    )
    return p


def _fixtures(tmp_path: Path) -> dict[str, bytes]:
    pofix = _targz(
        tmp_path / "pofix.tgz",
        [("pofix-9.9/.tool-versions", b"terraform 1.9.0\nhcledit 0.2.17\n")],
    )
    tf = _zip(tmp_path / "tf.zip", [("terraform", b"TFBINARY")])
    return {"pofix": pofix, "terraform": tf}


def _install_fake_download(monkeypatch, fixtures: dict[str, bytes]):
    def _fake(src, token, dest_path):
        data = fixtures[src.name]
        dest_path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(source_locker, "download_unverified", _fake)


def test_lock_resolves_version_from_and_repins(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    fixtures = _fixtures(tmp_path)
    _install_fake_download(monkeypatch, fixtures)

    changed = lock_sources(manifest, None)
    assert changed is True

    reloaded = load_manifest(manifest).lambdas[0]
    by_name = {s.name: s for s in reloaded.sources}
    assert by_name["terraform"].version == "1.9.0"  # derived from pofix .tool-versions
    assert by_name["terraform"].sha256 == hashlib.sha256(fixtures["terraform"]).hexdigest()
    assert by_name["pofix"].sha256 == hashlib.sha256(fixtures["pofix"]).hexdigest()
    assert by_name["pofix"].version == "9.9"  # root version untouched by lock


def test_lock_preserves_comments(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    _install_fake_download(monkeypatch, _fixtures(tmp_path))
    lock_sources(manifest, None)
    assert "# keep this comment" in manifest.read_text()


def test_lock_is_idempotent(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    _install_fake_download(monkeypatch, _fixtures(tmp_path))

    assert lock_sources(manifest, None) is True  # first run pins
    before = manifest.read_text()
    assert lock_sources(manifest, None) is False  # nothing to change
    assert manifest.read_text() == before  # byte-for-byte unchanged -> no PR


def test_lock_no_sources_returns_false(tmp_path):
    p = tmp_path / "lambdas.toml"
    p.write_text(
        "[[lambda]]\n"
        'logical_name      = "app"\n'
        'source_dir        = "h"\n'
        'requirements_lock = "h/requirements.${arch}.lock"\n'
        'runtime           = "python3.13"\n'
        'arch              = "arm64"\n'
        'handler           = "app.lambda_handler"\n'
        "\n[builder]\n"
        f'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:{"0" * 64}"\n'
    )
    assert lock_sources(p, None) is False
