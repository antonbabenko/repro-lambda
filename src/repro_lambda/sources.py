"""Fetch, verify, and extract pinned external sources (the declarative sources DSL).

Security model (defense in depth; see SETUP.md "Sources"):

- HTTPS only. A manual redirect loop resolves each hop's hostname to an IP, rejects any
  non-global-unicast / reserved / private / loopback / link-local address (SSRF guard),
  and connects to that pinned IP while verifying the TLS cert against the original
  hostname - so a DNS-rebind between validate and connect cannot occur.
- `Authorization` is stripped on any cross-host redirect (e.g. the GitHub API 302 to its
  S3/Blob asset host), so a private token never leaks to a redirect target.
- The downloaded bytes are sha256-verified BEFORE any archive is opened. The cache is
  keyed by that sha256 and re-verified on every use (anti cache-poisoning).
- Extraction rejects absolute / `..` / symlink / hardlink / device / fifo entries and
  bounds total bytes, entry count, per-entry bytes (decompression-bomb limits), writing
  to a temp dir then promoting into place. Members get a normalized mtime + perms.

`build` calls `fetch_sources` with concrete pins only (it never resolves versions);
`lock` is the only path that re-resolves `version_from` and re-pins sha256.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import ipaddress
import json
import os
import shutil
import socket
import ssl
import tarfile
import tempfile
import zipfile
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from repro_lambda import __version__
from repro_lambda.manifest import Source

# Limits (decompression-bomb + DoS bounds). Generous enough for real release artifacts.
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB compressed download cap
MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # 2 GiB extracted cap
MAX_PER_ENTRY_BYTES = 1024 * 1024 * 1024  # 1 GiB per archive entry
MAX_ARCHIVE_ENTRIES = 50_000
MAX_REDIRECTS = 5
HTTP_TIMEOUT = 60  # seconds per hop
_GITHUB_API = "https://api.github.com"
_JSON_MAX_BYTES = 16 * 1024 * 1024
_CHUNK = 64 * 1024
# The GitHub REST API rejects requests with no User-Agent (HTTP 403); send one on every
# request (harmless for the plain https sources, required for github_release resolution).
_USER_AGENT = f"repro-lambda/{__version__}"


class SourceFetchError(RuntimeError):
    """A source could not be fetched, verified, or extracted."""


class SourceSecurityError(SourceFetchError):
    """A fetch/extract was refused by a security guard (SSRF, traversal, bomb, mismatch)."""


# --- SSRF-safe HTTPS -------------------------------------------------------


def _redact(url: str) -> str:
    """URL safe for logs: scheme://host[:port]/path, dropping userinfo + query."""
    p = urlsplit(url)
    netloc = p.hostname or ""
    if p.port:
        netloc = f"{netloc}:{p.port}"
    return urlunsplit((p.scheme, netloc, p.path, "", ""))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_public_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # validate the embedded v4 of ::ffff:a.b.c.d
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_public_ip(host: str) -> str:
    """Resolve host to a single vetted public IP, or refuse. Connection pins this IP."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SourceFetchError(f"DNS resolution failed for {host!r}: {e}") from e
    for info in infos:
        ip = info[4][0]
        if _is_public_ip(ip):
            return ip
    raise SourceSecurityError(f"{host!r} resolves only to non-public addresses; refusing")


class _PinnedHTTPSConnection(HTTPSConnection):
    """HTTPSConnection that connects to a pre-validated IP but keeps the hostname for
    SNI + certificate verification (no second DNS lookup that a rebind could poison)."""

    def __init__(self, host: str, pinned_ip: str, **kw):
        super().__init__(host, **kw)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self.context.wrap_socket(sock, server_hostname=self.host)


def _http_request(
    url: str, headers: dict[str, str], sink: io.BufferedWriter, max_bytes: int
) -> str:
    """GET url following redirects under the SSRF guard; stream body into sink.

    Returns the sha256 of the streamed body. Strips Authorization on any cross-host hop.
    """
    origin_host = urlsplit(url).hostname
    context = ssl.create_default_context()
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme != "https":
            raise SourceSecurityError(f"non-https URL refused: {_redact(current)}")
        host = parts.hostname
        if not host or _is_ip_literal(host):
            raise SourceSecurityError(f"IP-literal or missing host refused: {_redact(current)}")
        pinned_ip = _resolve_public_ip(host)
        req_headers = dict(headers)
        if host != origin_host:  # crossed hosts (e.g. api.github.com -> codeload/S3)
            req_headers.pop("Authorization", None)
        conn = _PinnedHTTPSConnection(host, pinned_ip, port=parts.port or 443, timeout=HTTP_TIMEOUT)
        conn.context = context
        try:
            target = parts.path + (f"?{parts.query}" if parts.query else "")
            conn.request(
                "GET",
                target or "/",
                headers={"User-Agent": _USER_AGENT, **req_headers, "Host": host},
            )
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                resp.read()
                if not location:
                    raise SourceFetchError(f"redirect without Location from {_redact(current)}")
                current = urljoin(current, location)
                continue
            if resp.status != 200:
                raise SourceFetchError(f"GET {_redact(current)} -> HTTP {resp.status}")
            return _stream_to(resp, sink, max_bytes)
        finally:
            conn.close()
    raise SourceFetchError(f"too many redirects fetching {_redact(url)}")


def _stream_to(resp, sink: io.BufferedWriter, max_bytes: int) -> str:
    h = hashlib.sha256()
    total = 0
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SourceSecurityError(f"download exceeds {max_bytes} bytes; aborting")
        h.update(chunk)
        sink.write(chunk)
    return h.hexdigest()


def _http_get_json(url: str, headers: dict[str, str]) -> dict:
    buf = io.BytesIO()
    writer = io.BufferedWriter(_RawSink(buf))
    _http_request(url, headers, writer, _JSON_MAX_BYTES)
    writer.flush()
    return json.loads(buf.getvalue().decode("utf-8"))


class _RawSink(io.RawIOBase):
    """Adapt a BytesIO to the BufferedWriter sink interface used by _http_request."""

    def __init__(self, buf: io.BytesIO):
        self._buf = buf

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        return self._buf.write(b)


# --- GitHub release asset resolution --------------------------------------


def _github_asset_url(src: Source, token: str | None) -> tuple[str, dict[str, str]]:
    """Resolve a github_release source to its asset download URL + auth headers."""
    if not token:
        raise SourceFetchError(
            f"source {src.name!r} is github_release but no token provided "
            f"(set REPRO_LAMBDA_SOURCES_TOKEN)"
        )
    tag = src.resolved_tag
    api = f"{_GITHUB_API}/repos/{src.repo}/releases/tags/{tag}"
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    release = _http_get_json(api, auth)
    want = src.resolved_asset
    for asset in release.get("assets", []):
        if asset.get("name") == want:
            asset_id = asset["id"]
            dl = f"{_GITHUB_API}/repos/{src.repo}/releases/assets/{asset_id}"
            return dl, {"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"}
    raise SourceFetchError(f"source {src.name!r}: asset {want!r} not found in {src.repo}@{tag}")


# --- download + cache ------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_verified(src: Source, cache_dir: Path, token: str | None, tmp: Path) -> Path:
    """Return a path to the sha256-verified raw bytes for src (cache hit or download)."""
    cached = cache_dir / f"{src.sha256}.bin"
    if cached.is_file():
        if _sha256_file(cached) == src.sha256:  # re-verify every use (anti-poisoning)
            return cached
        cached.unlink()  # corrupt/poisoned cache entry; re-download

    if src.type == "github_release":
        url, headers = _github_asset_url(src, token)
    else:
        url, headers = src.resolved_url, {}

    staging = tmp / f"dl-{src.name}"
    with staging.open("wb") as raw:  # binary "wb" handle is already a buffered writer
        actual = _http_request(url, headers, raw, MAX_DOWNLOAD_BYTES)
    if actual != src.sha256:
        staging.unlink(missing_ok=True)
        raise SourceSecurityError(
            f"source {src.name!r} sha256 mismatch: expected {src.sha256}, got {actual} "
            f"(from {_redact(url)})"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.replace(staging, cached)
    return cached


# --- safe extraction -------------------------------------------------------


def _safe_join(base: Path, rel: str) -> Path:
    base = base.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise SourceSecurityError(f"path escapes destination: {rel!r}")
    return target


def _reject_unsafe_name(name: str) -> None:
    if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
        raise SourceSecurityError(f"unsafe archive entry name: {name!r}")


def _copy_capped(src, dst, limit: int) -> None:
    written = 0
    while True:
        chunk = src.read(_CHUNK)
        if not chunk:
            break
        written += len(chunk)
        if written > limit:
            raise SourceSecurityError("archive entry exceeds declared/allowed size (bomb)")
        dst.write(chunk)


def _normalize(path: Path, executable: bool) -> None:
    mode = 0o755 if executable else 0o644
    path.chmod(mode)
    os.utime(path, (0, 0))


class _LimitingReader(io.RawIOBase):
    """Bounds total bytes pulled through a (decompressed) stream - tar-bomb guard."""

    def __init__(self, fileobj, max_bytes: int):
        self._f = fileobj
        self._max = max_bytes
        self._count = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self._f.read(size)
        self._count += len(chunk)
        if self._count > self._max:
            raise SourceSecurityError("decompressed size exceeds limit (tar bomb)")
        return chunk

    def readinto(self, b) -> int:
        chunk = self.read(len(b))
        n = len(chunk)
        b[:n] = chunk
        return n


def _extract_zip(raw_path: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(raw_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise SourceSecurityError(f"archive has too many entries ({len(infos)})")
        total = 0
        for info in sorted(infos, key=lambda i: i.filename):
            _reject_unsafe_name(info.filename)
            if _zip_is_symlink(info):
                raise SourceSecurityError(f"symlink entry rejected: {info.filename!r}")
            target = _safe_join(out_dir, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.file_size > MAX_PER_ENTRY_BYTES:
                raise SourceSecurityError(f"entry too large: {info.filename!r}")
            total += info.file_size
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise SourceSecurityError("archive exceeds total uncompressed limit (bomb)")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                _copy_capped(src, dst, min(info.file_size, MAX_PER_ENTRY_BYTES))
            _normalize(target, executable=False)


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode lives in the high 16 bits of external_attr; S_IFLNK == 0o120000.
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _extract_targz(raw_path: Path, out_dir: Path) -> None:
    with raw_path.open("rb") as raw, gzip.GzipFile(fileobj=raw) as gz:
        limited = _LimitingReader(gz, MAX_TOTAL_UNCOMPRESSED)
        count = 0
        with tarfile.open(fileobj=limited, mode="r|") as tf:  # streaming (no seek/backref)
            for member in tf:
                count += 1
                if count > MAX_ARCHIVE_ENTRIES:
                    raise SourceSecurityError("archive has too many entries")
                _reject_unsafe_name(member.name)
                if member.issym() or member.islnk():
                    raise SourceSecurityError(f"link entry rejected: {member.name!r}")
                if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise SourceSecurityError(f"special entry rejected: {member.name!r}")
                target = _safe_join(out_dir, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise SourceSecurityError(f"unsupported entry type: {member.name!r}")
                if member.size > MAX_PER_ENTRY_BYTES:
                    raise SourceSecurityError(f"entry too large: {member.name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    raise SourceFetchError(f"could not read entry: {member.name!r}")
                with extracted as src, target.open("wb") as dst:
                    _copy_capped(src, dst, min(member.size, MAX_PER_ENTRY_BYTES))
                _normalize(target, executable=False)


def _extract_to_temp(raw_path: Path, kind: str, parent: Path) -> Path:
    out = parent / "extracted"
    out.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        _extract_zip(raw_path, out)
    elif kind == "tar.gz":
        _extract_targz(raw_path, out)
    else:
        raise SourceFetchError(f"unknown extract kind: {kind!r}")
    return out


# --- staging into the package ----------------------------------------------


def _stage(src: Source, raw_path: Path, extracted: Path | None, dest_root: Path) -> None:
    target = _safe_join(dest_root, src.dest)
    if target.exists():  # source-vs-source or source-vs-source_dir collision
        raise SourceFetchError(
            f"source {src.name!r} dest {src.dest!r} collides with already-staged content"
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    if src.extract == "none":
        shutil.copyfile(raw_path, target)
        _normalize(target, src.executable)
    elif src.member:
        member_path = _safe_join(extracted, src.resolved_member)  # type: ignore[arg-type]
        if member_path.is_dir():  # map a (versioned) subtree to dest
            shutil.copytree(member_path, target)
        elif member_path.is_file():
            shutil.copyfile(member_path, target)
            _normalize(target, src.executable)
        else:
            raise SourceFetchError(
                f"source {src.name!r}: member {src.resolved_member!r} not found in archive"
            )
    else:
        shutil.copytree(extracted, target)  # type: ignore[arg-type]


def download_unverified(src: Source, token: str | None, dest_path: Path) -> str:
    """Download a source's resolved artifact (no sha pin) and return its sha256.

    Used by `lock`, which is computing the new pin and therefore has no sha to verify
    against yet. The SSRF guard, redirect auth-strip, and size cap still apply.
    """
    if src.type == "github_release":
        url, headers = _github_asset_url(src, token)
    else:
        url, headers = src.resolved_url, {}
    with dest_path.open("wb") as raw:
        return _http_request(url, headers, raw, MAX_DOWNLOAD_BYTES)


def extract_to_temp(raw_path: Path, kind: str, parent: Path) -> Path:
    """Public wrapper so `lock` can extract a referenced source to read version_from."""
    return _extract_to_temp(raw_path, kind, parent)


def fetch_sources(
    *,
    sources: tuple[Source, ...],
    dest_root: Path,
    cache_dir: Path,
    github_token: str | None,
) -> None:
    """Fetch + verify + extract each source into dest_root/<dest>. Concrete pins only.

    All-or-none in practice: any failure raises, and the caller stages into an ephemeral
    tree that is discarded on error. Sources are post-filter (include/exclude do not apply).
    """
    if not sources:
        return
    with tempfile.TemporaryDirectory(prefix="repro-sources-") as td:
        tmp = Path(td)
        for src in sources:
            if not src.sha256:
                raise SourceFetchError(
                    f"source {src.name!r} has no pinned sha256; run `repro-lambda lock` first"
                )
            raw = _fetch_verified(src, cache_dir, github_token, tmp)
            extracted = None
            if src.extract != "none":
                extracted = _extract_to_temp(raw, src.extract, tmp / f"x-{src.name}")
            _stage(src, raw, extracted, dest_root)
