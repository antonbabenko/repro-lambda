"""Pack a staged directory into a byte-reproducible zip."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

_EPOCH = (1980, 1, 1, 0, 0, 0)


def _should_exclude(rel: str, exclude_glob: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in exclude_glob)


def _zip_mode(path: Path) -> int:
    src_mode = path.stat().st_mode
    if path.is_dir():
        return 0o755
    if src_mode & 0o111 or path.suffix in {".so", ".node"}:
        return 0o755
    return 0o644


def pack_directory(
    src: Path,
    out: Path,
    *,
    exclude_glob: list[str] | None = None,
) -> None:
    """
    Pack `src` recursively into `out` as a deterministic zip.

    Reproducibility properties set explicitly on every entry:
      - Entries sorted alphabetically by POSIX relpath
      - mtime forced to 1980-01-01 00:00:00
      - Directory entries 0o755; files 0o644 (or 0o755 if executable / .so / .node)
      - create_system = 3 (unix) so external_attr is interpreted as unix mode

    Uses stdlib zipfile directly because repro_zipfile normalizes external_attr in a
    way that strips per-file mode bits we need to preserve.
    """
    exclude_glob = exclude_glob or []

    paths: list[Path] = []
    for p in src.rglob("*"):
        rel = p.relative_to(src).as_posix()
        if _should_exclude(rel, exclude_glob):
            continue
        paths.append(p)

    paths.sort(key=lambda p: p.relative_to(src).as_posix())

    out.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as zf:
        for p in paths:
            rel = p.relative_to(src).as_posix()
            zinfo = ZipInfo(
                filename=(rel + "/") if p.is_dir() else rel,
                date_time=_EPOCH,
            )
            zinfo.create_system = 3
            mode = _zip_mode(p)
            zinfo.external_attr = (mode & 0xFFFF) << 16
            if p.is_dir():
                zinfo.external_attr |= 0x10
                data = b""
            else:
                data = p.read_bytes()
            zf.writestr(zinfo, data)
