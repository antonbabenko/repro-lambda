"""lock_sources: version_from resolution, sha re-pin, idempotent tomlkit rewrite."""

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from repro_lambda import source_locker
from repro_lambda.manifest import load_manifest
from repro_lambda.source_locker import lock_sources
from repro_lambda.sources import SourceFetchError

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


def _manifest(tmp_path: Path, pin_file: str = ".tool-versions", pin_format: str = "asdf") -> Path:
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
        'version = "0.0.0"\n'  # stale; lock derives it from the pofix pin file
        "[lambda.source.version_from]\n"
        'source = "pofix"\n'
        f'file   = "{pin_file}"\n'
        f'format = "{pin_format}"\n'
        'key    = "terraform"\n'
        "\n[builder]\n"
        f'base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:{"0" * 64}"\n'
    )
    return p


ASDF_PINS = b"terraform 1.9.0\nhcledit 0.2.17\n"

# A string pin, a table pin, and an entry that pins no version at all.
MISE_PINS = b"""[tools]
"aqua:hashicorp/terraform" = "1.9.0"

[tools."http:hcledit"]
version = "0.2.17"

[tools."http:nopin"]
bin_path = "bin"
"""


def _fixtures(
    tmp_path: Path, pin_name: str = ".tool-versions", pin_body: bytes = ASDF_PINS
) -> dict[str, bytes]:
    pofix = _targz(tmp_path / "pofix.tgz", [(f"pofix-9.9/{pin_name}", pin_body)])
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


def test_lock_resolves_version_from_mise(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, pin_file="mise.toml", pin_format="mise")
    _install_fake_download(monkeypatch, _fixtures(tmp_path, "mise.toml", MISE_PINS))

    assert lock_sources(manifest, None) is True

    by_name = {s.name: s for s in load_manifest(manifest).lambdas[0].sources}
    assert by_name["terraform"].version == "1.9.0"  # short name of "aqua:hashicorp/terraform"


def test_lock_mise_missing_key_raises(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, pin_file="mise.toml", pin_format="mise")
    pins = MISE_PINS.replace(b'"aqua:hashicorp/terraform" = "1.9.0"', b"")
    _install_fake_download(monkeypatch, _fixtures(tmp_path, "mise.toml", pins))

    with pytest.raises(SourceFetchError, match="key 'terraform' not found"):
        lock_sources(manifest, None)
