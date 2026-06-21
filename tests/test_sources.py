"""Security + behavior tests for sources.py (SSRF guard, extractor, cache, staging)."""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from repro_lambda import sources
from repro_lambda.manifest import Source
from repro_lambda.sources import (
    SourceFetchError,
    SourceSecurityError,
    fetch_sources,
)

SHA_PLACEHOLDER = "a" * 64


# --- IP / host guards ------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",
        "2606:4700:4700::1111",
    ],
)
def test_public_ips_allowed(ip):
    assert sources._is_public_ip(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata
        "10.0.0.1",
        "172.16.5.5",
        "192.168.1.1",
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",  # IPv4-mapped metadata
    ],
)
def test_reserved_ips_refused(ip):
    assert sources._is_public_ip(ip) is False


def test_is_ip_literal():
    assert sources._is_ip_literal("1.2.3.4") is True
    assert sources._is_ip_literal("::1") is True
    assert sources._is_ip_literal("example.com") is False


def test_redact_strips_userinfo_and_query():
    red = sources._redact("https://user:pw@host.example/path?token=secret&x=1")
    assert red == "https://host.example/path"
    assert "secret" not in red and "user" not in red


def test_resolve_public_ip_refuses_private(monkeypatch):
    monkeypatch.setattr(
        sources.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))],
    )
    with pytest.raises(SourceSecurityError, match="non-public"):
        sources._resolve_public_ip("evil.example")


def test_resolve_public_ip_returns_public(monkeypatch):
    monkeypatch.setattr(
        sources.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443)), (2, 1, 6, "", ("8.8.8.8", 443))],
    )
    assert sources._resolve_public_ip("ok.example") == "8.8.8.8"


# --- redirect loop: auth strip + scheme/ip refusal -------------------------


class _FakeResp:
    def __init__(self, status, location=None, body=b""):
        self.status = status
        self._location = location
        self._chunks = [body, b""] if body else [b""]

    def getheader(self, key):
        return self._location if key == "Location" else None

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeConn:
    calls: list["_FakeConn"] = []
    responses: list[_FakeResp] = []

    def __init__(self, host, pinned_ip, port=443, timeout=None):
        self.host = host
        self.context = None
        self.sent_headers = None
        _FakeConn.calls.append(self)
        self._resp = _FakeConn.responses.pop(0)

    def request(self, method, target, headers):
        self.sent_headers = dict(headers)

    def getresponse(self):
        return self._resp

    def close(self):
        pass


@pytest.fixture
def fake_https(monkeypatch):
    monkeypatch.setattr(sources, "_resolve_public_ip", lambda host: "8.8.8.8")
    monkeypatch.setattr(sources, "_PinnedHTTPSConnection", _FakeConn)
    _FakeConn.calls = []
    _FakeConn.responses = []
    return _FakeConn


def test_authorization_stripped_on_cross_host_redirect(fake_https):
    fake_https.responses = [
        _FakeResp(302, location="https://objects.example.com/asset"),
        _FakeResp(200, body=b"PAYLOAD"),
    ]
    sink = io.BytesIO()
    sha = sources._http_request(
        "https://api.github.com/repos/o/r/releases/assets/9",
        {"Authorization": "Bearer SECRET", "Accept": "application/octet-stream"},
        sink,
        1 << 20,
    )
    assert sink.getvalue() == b"PAYLOAD"
    assert sha == __import__("hashlib").sha256(b"PAYLOAD").hexdigest()
    assert fake_https.calls[0].host == "api.github.com"
    assert fake_https.calls[0].sent_headers["Authorization"] == "Bearer SECRET"
    assert "Authorization" not in fake_https.calls[1].sent_headers  # leaked? no.


def test_authorization_kept_on_same_host_redirect(fake_https):
    fake_https.responses = [
        _FakeResp(302, location="https://api.github.com/second"),
        _FakeResp(200, body=b"X"),
    ]
    sources._http_request(
        "https://api.github.com/first", {"Authorization": "Bearer SECRET"}, io.BytesIO(), 1 << 20
    )
    assert fake_https.calls[1].sent_headers["Authorization"] == "Bearer SECRET"


def test_non_https_refused():
    with pytest.raises(SourceSecurityError, match="non-https"):
        sources._http_request("http://x.example/a", {}, io.BytesIO(), 1 << 20)


def test_ip_literal_url_refused():
    with pytest.raises(SourceSecurityError, match="IP-literal"):
        sources._http_request("https://93.184.216.34/a", {}, io.BytesIO(), 1 << 20)


def test_too_many_redirects(fake_https):
    fake_https.responses = [
        _FakeResp(302, location=f"https://api.github.com/{i}")
        for i in range(sources.MAX_REDIRECTS + 1)
    ]
    with pytest.raises(SourceFetchError, match="too many redirects"):
        sources._http_request("https://api.github.com/0", {}, io.BytesIO(), 1 << 20)


def test_download_size_cap(fake_https):
    fake_https.responses = [_FakeResp(200, body=b"X" * 100)]
    with pytest.raises(SourceSecurityError, match="exceeds"):
        sources._http_request("https://api.github.com/big", {}, io.BytesIO(), 10)


# --- zip extraction guards -------------------------------------------------


def _zip(path: Path, entries: list[tuple[str, bytes]], *, symlink: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16  # S_IFLNK
            zf.writestr(info, "target")
    return path


def test_zip_happy_extract(tmp_path: Path):
    arc = _zip(tmp_path / "a.zip", [("dir/f.txt", b"hi"), ("g.bin", b"\x00\x01")])
    out = tmp_path / "out"
    out.mkdir()
    sources._extract_zip(arc, out)
    assert (out / "dir" / "f.txt").read_bytes() == b"hi"
    assert (out / "g.bin").read_bytes() == b"\x00\x01"
    assert (out / "dir" / "f.txt").stat().st_mode & 0o777 == 0o644


def test_zip_rejects_traversal(tmp_path: Path):
    arc = _zip(tmp_path / "a.zip", [("../evil", b"x")])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SourceSecurityError, match="unsafe archive entry"):
        sources._extract_zip(arc, out)


def test_zip_rejects_absolute(tmp_path: Path):
    arc = _zip(tmp_path / "a.zip", [("/etc/passwd", b"x")])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SourceSecurityError, match="unsafe archive entry"):
        sources._extract_zip(arc, out)


def test_zip_rejects_symlink(tmp_path: Path):
    arc = _zip(tmp_path / "a.zip", [("ok.txt", b"x")], symlink="link")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SourceSecurityError, match="symlink entry rejected"):
        sources._extract_zip(arc, out)


def test_zip_total_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_TOTAL_UNCOMPRESSED", 5)
    arc = _zip(tmp_path / "a.zip", [("a", b"123"), ("b", b"456")])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SourceSecurityError, match="total uncompressed"):
        sources._extract_zip(arc, out)


# --- tar.gz extraction guards ----------------------------------------------


def _targz(
    path: Path, files: list[tuple[str, bytes]], *, link: tuple[str, str] | None = None
) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if link is not None:
            name, target = link
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return path


def test_targz_happy_extract(tmp_path: Path):
    arc = _targz(tmp_path / "a.tgz", [("pofix/.tool-versions", b"terraform 1.9.0\n")])
    out = tmp_path / "out"
    out.mkdir()
    sources._extract_targz(arc, out)
    assert (out / "pofix" / ".tool-versions").read_bytes() == b"terraform 1.9.0\n"


def test_targz_rejects_traversal(tmp_path: Path):
    arc = _targz(tmp_path / "a.tgz", [("../evil", b"x")])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SourceSecurityError, match="unsafe archive entry"):
        sources._extract_targz(arc, out)


def test_targz_rejects_symlink(tmp_path: Path):
    arc = _targz(tmp_path / "a.tgz", [("ok", b"x")], link=("link", "/etc/passwd"))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SourceSecurityError, match="link entry rejected"):
        sources._extract_targz(arc, out)


def test_targz_bomb_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_TOTAL_UNCOMPRESSED", 8)
    arc = _targz(tmp_path / "a.tgz", [("big", b"X" * 64)])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SourceSecurityError, match="tar bomb"):
        sources._extract_targz(arc, out)


def test_safe_join_escape(tmp_path: Path):
    with pytest.raises(SourceSecurityError, match="escapes destination"):
        sources._safe_join(tmp_path, "../outside")


# --- cache + verify --------------------------------------------------------


def test_fetch_verified_cache_hit_no_network(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = b"cached-bytes"
    sha = __import__("hashlib").sha256(payload).hexdigest()
    (cache / f"{sha}.bin").write_bytes(payload)
    src = Source(name="x", type="https", sha256=sha, extract="none", dest="x", url="https://e/a")

    def _boom(*a, **k):
        raise AssertionError("network must not be touched on a verified cache hit")

    monkeypatch.setattr(sources, "_http_request", _boom)
    got = sources._fetch_verified(src, cache, None, tmp_path)
    assert got.read_bytes() == payload


def test_fetch_verified_sha_mismatch_raises(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"

    def _fake_download(url, headers, sink, max_bytes):
        sink.write(b"unexpected")
        return "f" * 64  # not the pinned sha

    monkeypatch.setattr(sources, "_http_request", _fake_download)
    src = Source(
        name="x", type="https", sha256=SHA_PLACEHOLDER, extract="none", dest="x", url="https://e/a"
    )
    with pytest.raises(SourceSecurityError, match="sha256 mismatch"):
        sources._fetch_verified(src, cache, None, tmp_path)


def test_github_source_requires_token(tmp_path: Path):
    src = Source(
        name="p",
        type="github_release",
        sha256=SHA_PLACEHOLDER,
        extract="tar.gz",
        dest="p",
        repo="o/r",
        tag="t",
        asset="a.tgz",
    )
    with pytest.raises(SourceFetchError, match="no token provided"):
        sources._github_asset_url(src, None)


# --- staging + end-to-end (no network) -------------------------------------


def test_fetch_sources_stages_dir_member_and_file_member(tmp_path: Path, monkeypatch):
    # Real shape: pofix tarball has a versioned top dir; a dir-member maps it to dest.
    # terraform zip has the bare binary at root; a file-member maps it to dest.
    tgz = _targz(
        tmp_path / "pofix.tgz",
        [("pofix-9.9/data/x.json", b"{}"), ("pofix-9.9/.tool-versions", b"v")],
    )
    zp = _zip(tmp_path / "tf.zip", [("terraform", b"BINARY")])
    fixtures = {"pofix": tgz, "tf": zp}
    monkeypatch.setattr(sources, "_fetch_verified", lambda src, c, t, tmp: fixtures[src.name])

    srcs = (
        Source(
            name="pofix",
            type="https",
            sha256=SHA_PLACEHOLDER,
            extract="tar.gz",
            member="pofix-{version}",
            dest="pofix",
            version="9.9",
            url="https://e/p",
            version_from=None,
        ),
        Source(
            name="tf",
            type="https",
            sha256="b" * 64,
            extract="zip",
            member="terraform",
            dest="bin/terraform",
            executable=True,
            url="https://e/t",
        ),
    )
    dest = tmp_path / "pkg"
    dest.mkdir()
    fetch_sources(sources=srcs, dest_root=dest, cache_dir=tmp_path / "c", github_token=None)

    assert (dest / "pofix" / "data" / "x.json").read_bytes() == b"{}"
    assert (dest / "pofix" / ".tool-versions").read_bytes() == b"v"
    assert (dest / "bin" / "terraform").read_bytes() == b"BINARY"
    assert (dest / "bin" / "terraform").stat().st_mode & 0o111  # +x


def test_fetch_sources_whole_tree_no_member(tmp_path: Path, monkeypatch):
    tgz = _targz(tmp_path / "t.tgz", [("a/b.txt", b"x")])
    monkeypatch.setattr(sources, "_fetch_verified", lambda src, c, t, tmp: tgz)
    src = Source(
        name="t",
        type="https",
        sha256=SHA_PLACEHOLDER,
        extract="tar.gz",
        dest="vendor",
        url="https://e/t",
    )
    dest = tmp_path / "pkg"
    dest.mkdir()
    fetch_sources(sources=(src,), dest_root=dest, cache_dir=tmp_path / "c", github_token=None)
    assert (dest / "vendor" / "a" / "b.txt").read_bytes() == b"x"


def test_fetch_sources_collision_refused(tmp_path: Path, monkeypatch):
    zp = _zip(tmp_path / "t.zip", [("terraform", b"X")])
    monkeypatch.setattr(sources, "_fetch_verified", lambda src, c, t, tmp: zp)
    dest = tmp_path / "pkg"
    (dest / "bin").mkdir(parents=True)
    (dest / "bin" / "terraform").write_bytes(b"already here")  # pre-existing (e.g. from source_dir)
    src = Source(
        name="tf",
        type="https",
        sha256="b" * 64,
        extract="zip",
        member="terraform",
        dest="bin/terraform",
        url="https://e/t",
    )
    with pytest.raises(SourceFetchError, match="collides"):
        fetch_sources(sources=(src,), dest_root=dest, cache_dir=tmp_path / "c", github_token=None)


def test_fetch_sources_requires_locked_sha(tmp_path: Path):
    src = Source(name="x", type="https", sha256="", extract="none", dest="x", url="https://e/a")
    with pytest.raises(SourceFetchError, match="run `repro-lambda lock`"):
        fetch_sources(
            sources=(src,), dest_root=tmp_path, cache_dir=tmp_path / "c", github_token=None
        )
