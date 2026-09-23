"""Guards for HASH_CONTRACT: artifact keys must change exactly when packaged bytes can."""

from __future__ import annotations

import hashlib
from pathlib import Path

import repro_lambda
from repro_lambda.hasher import HASH_CONTRACT, compute_content_hash
from repro_lambda.manifest import LambdaSpec

_PKG = Path(repro_lambda.__file__).parent

# Modules that shape the zip bytes beyond the hashed inputs. Editing one means deciding
# whether the same inputs can now produce different bytes: if yes, bump HASH_CONTRACT;
# either way, record the new fingerprints here under the (possibly new) contract.
PACKAGING_FINGERPRINTS = {
    "0.8.0": {
        "zip_packager.py": "56810a5e337a2fd5f72c269ba81bab06ca2f019e14e0edb728d6c37881d7cf0a",
        "docker_runner.py": "0092a103b8dd11e6c0a00979e4c38272ccc6d707f2ee74035d8d84e61fed4988",
        "source_stager.py": "38186277fb94ab6feac8a07cad2ae35f5adfaa52bde3d51ac71daccb003d3c68",
        "sources.py": "10415ccc89b85f521fb64ef199f084f0bea1842032d414cb3d3537334f6caa0b",
    },
}


def test_packaging_modules_match_the_hash_contract():
    expected = PACKAGING_FINGERPRINTS.get(HASH_CONTRACT)
    assert expected, f"no packaging fingerprints recorded for HASH_CONTRACT={HASH_CONTRACT!r}"
    actual = {name: hashlib.sha256((_PKG / name).read_bytes()).hexdigest() for name in expected}
    changed = sorted(n for n in expected if actual[n] != expected[n])
    assert not changed, (
        f"packaging module(s) changed: {changed}. If the same inputs can now produce "
        "different zip bytes, bump HASH_CONTRACT in hasher.py. Then record these "
        f"fingerprints under the contract: {actual}"
    )


def test_content_hash_golden_key(tmp_path: Path):
    # Pins the hash layout: any change to what is folded, or how, re-keys every artifact.
    src = tmp_path / "source"
    src.mkdir()
    (src / "app.py").write_text("ok\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("\n")
    spec = LambdaSpec(
        logical_name="fn",
        source_dir="src/fn",
        requirements_lock="src/fn/requirements.${arch}.lock",
        runtime="python3.13",
        arch="arm64",
        handler="app.handler",
        region="eu-west-1",
    )
    got = compute_content_hash(src, lock, spec, "img@sha256:0", HASH_CONTRACT)
    assert got == "de8da0c8801288758e8dc86edc15b085ffadc6eb915dcc45d875bfb235854dea"


# The zip command's exclusion list lives in cli.py; changing it changes the zip bytes.
ZIP_EXCLUDES = {
    "0.8.0": [
        "*__pycache__*",
        "*.pyc",
        "*.dist-info/RECORD",
        "*.dist-info/INSTALLER",
        "*.dist-info/direct_url.json",
        "*.dist-info/REQUESTED",
    ],
}


def test_zip_excludes_match_the_hash_contract():
    from repro_lambda.cli import _LAMBDA_ZIP_EXCLUDES

    assert _LAMBDA_ZIP_EXCLUDES == ZIP_EXCLUDES.get(HASH_CONTRACT), (
        "the lambda zip exclusion list changed: bump HASH_CONTRACT in hasher.py "
        "and record the new list under the contract"
    )
