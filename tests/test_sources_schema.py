"""Schema parse + validation for [[lambda.source]] (declarative sources DSL)."""

from pathlib import Path

import pytest

from repro_lambda.manifest import Source, VersionFrom, load_manifest

PINNED_IMAGE = "public.ecr.aws/lambda/python:3.13@sha256:" + "0" * 64
SHA = "a" * 64
SHA2 = "b" * 64


def _manifest(tmp_path: Path, sources_toml: str) -> Path:
    p = tmp_path / "lambdas.toml"
    p.write_text(
        "[[lambda]]\n"
        'logical_name      = "app"\n'
        'source_dir        = "handler"\n'
        'requirements_lock = "handler/requirements.${arch}.lock"\n'
        'runtime           = "python3.13"\n'
        'arch              = "arm64"\n'
        'handler           = "app.lambda_handler"\n'
        f"{sources_toml}"
        "\n[builder]\n"
        f'base_image_python = "{PINNED_IMAGE}"\n'
    )
    return p


def _load(tmp_path: Path, sources_toml: str) -> tuple[Source, ...]:
    return load_manifest(_manifest(tmp_path, sources_toml)).lambdas[0].sources


# --- happy path ------------------------------------------------------------


def test_parse_https_source(tmp_path: Path):
    sources = _load(
        tmp_path,
        "[[lambda.source]]\n"
        'name    = "tf"\n'
        'type    = "https"\n'
        'url     = "https://releases.example.com/tf_1.0_linux_arm64.zip"\n'
        f'sha256  = "{SHA}"\n'
        'extract = "zip"\n'
        'member  = "terraform"\n'
        'dest    = "bin/terraform"\n'
        "executable = true\n",
    )
    assert sources == (
        Source(
            name="tf",
            type="https",
            sha256=SHA,
            extract="zip",
            dest="bin/terraform",
            url="https://releases.example.com/tf_1.0_linux_arm64.zip",
            member="terraform",
            executable=True,
        ),
    )


def test_parse_github_release_source(tmp_path: Path):
    sources = _load(
        tmp_path,
        "[[lambda.source]]\n"
        'name    = "pofix"\n'
        'type    = "github_release"\n'
        'repo    = "owner/pofix"\n'
        'tag     = "pofix-v0.10.0"\n'
        'asset   = "pofix-0.10.0-lambda.tar.gz"\n'
        f'sha256  = "{SHA}"\n'
        'extract = "tar.gz"\n'
        'dest    = "pofix"\n',
    )
    s = sources[0]
    assert s.type == "github_release"
    assert s.repo == "owner/pofix"
    assert s.member is None and s.executable is False


def test_parse_version_from_and_substitution(tmp_path: Path):
    sources = _load(
        tmp_path,
        "[[lambda.source]]\n"
        'name="pofix"\ntype="github_release"\nrepo="o/pofix"\ntag="pofix-v0.10.0"\n'
        f'asset="pofix-0.10.0.tar.gz"\nsha256="{SHA}"\nextract="tar.gz"\ndest="pofix"\n'
        "[[lambda.source]]\n"
        'name="tf"\ntype="https"\n'
        'url="https://releases.example.com/terraform_{version}_linux_arm64.zip"\n'
        f'sha256="{SHA2}"\nextract="zip"\nmember="terraform"\ndest="bin/terraform"\n'
        'version="1.9.0"\n'
        '[lambda.source.version_from]\nsource="pofix"\nfile=".tool-versions"\nkey="terraform"\n',
    )
    tf = sources[1]
    assert tf.version_from == VersionFrom(source="pofix", file=".tool-versions", key="terraform")
    assert tf.resolved_url == "https://releases.example.com/terraform_1.9.0_linux_arm64.zip"


def test_sources_default_empty(tmp_path: Path):
    assert _load(tmp_path, "") == ()


def test_sha256_may_be_empty_pre_lock(tmp_path: Path):
    sources = _load(
        tmp_path,
        '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a.zip"\n'
        'sha256=""\nextract="none"\ndest="x"\n',
    )
    assert sources[0].sha256 == ""


# --- validation: rejects ---------------------------------------------------


@pytest.mark.parametrize(
    ("toml", "match"),
    [
        (
            '[[lambda.source]]\ntype="https"\nurl="https://e.com/a"\nextract="none"\ndest="x"\n',
            "non-empty 'name'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="ftp"\nurl="https://e.com/a"\nextract="none"\ndest="x"\n',
            "type must be one of",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nextract="rar"\ndest="x"\n',
            "extract must be one of",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nextract="none"\ndest=""\n',
            "non-empty 'dest'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nextract="none"\ndest="/abs"\n',
            "without '..'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nextract="none"\ndest="../up"\n',
            "without '..'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nsha256="nothex"\nextract="none"\ndest="x"\n',
            "64 lowercase hex",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nextract="none"\nmember="m"\ndest="x"\n',
            "extract='none'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="github_release"\ntag="t"\nasset="a"\nextract="none"\ndest="x"\n',
            "requires non-empty 'repo'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="github_release"\nrepo="bad"\ntag="t"\nasset="a"\nextract="none"\ndest="x"\n',
            "must be 'owner/name'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="github_release"\nrepo="o/r"\ntag="t"\nasset="a"\nurl="https://e.com/a"\nextract="none"\ndest="x"\n',
            "must not set 'url'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nextract="none"\ndest="x"\n',
            "requires non-empty 'url'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="http://e.com/a"\nextract="none"\ndest="x"\n',
            "must start with https://",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nrepo="o/r"\nextract="none"\ndest="x"\n',
            "must not set 'repo'",
        ),
        (
            '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/{version}"\nextract="none"\ndest="x"\n',
            "no 'version' value or",
        ),
    ],
)
def test_rejects_invalid_source(tmp_path: Path, toml: str, match: str):
    with pytest.raises(ValueError, match=match):
        _load(tmp_path, toml)


def test_rejects_duplicate_name(tmp_path: Path):
    toml = (
        '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/a"\nextract="none"\ndest="a"\n'
        '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/b"\nextract="none"\ndest="b"\n'
    )
    with pytest.raises(ValueError, match="duplicate source name"):
        _load(tmp_path, toml)


def test_rejects_version_from_self_reference(tmp_path: Path):
    toml = (
        '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/{version}"\n'
        'extract="none"\ndest="a"\n'
        '[lambda.source.version_from]\nsource="x"\nfile="f"\nkey="k"\n'
    )
    with pytest.raises(ValueError, match="cannot reference itself"):
        _load(tmp_path, toml)


def test_rejects_version_from_unknown_source(tmp_path: Path):
    toml = (
        '[[lambda.source]]\nname="x"\ntype="https"\nurl="https://e.com/{version}"\n'
        'extract="none"\ndest="a"\n'
        '[lambda.source.version_from]\nsource="ghost"\nfile="f"\nkey="k"\n'
    )
    with pytest.raises(ValueError, match="not a defined source"):
        _load(tmp_path, toml)


def test_rejects_two_level_version_from(tmp_path: Path):
    toml = (
        '[[lambda.source]]\nname="a"\ntype="https"\nurl="https://e.com/{version}"\n'
        'extract="none"\ndest="a"\n'
        '[lambda.source.version_from]\nsource="b"\nfile="f"\nkey="k"\n'
        '[[lambda.source]]\nname="b"\ntype="https"\nurl="https://e.com/{version}"\n'
        'extract="none"\ndest="b"\n'
        '[lambda.source.version_from]\nsource="c"\nfile="f"\nkey="k"\n'
        '[[lambda.source]]\nname="c"\ntype="https"\nurl="https://e.com/x"\n'
        'extract="none"\ndest="c"\n'
    )
    with pytest.raises(ValueError, match="single-level only"):
        _load(tmp_path, toml)


def test_rejects_dest_tree_overlap(tmp_path: Path):
    toml = (
        '[[lambda.source]]\nname="a"\ntype="https"\nurl="https://e.com/a"\nextract="none"\ndest="bin"\n'
        '[[lambda.source]]\nname="b"\ntype="https"\nurl="https://e.com/b"\nextract="none"\ndest="bin/tf"\n'
    )
    with pytest.raises(ValueError, match="dest overlap"):
        _load(tmp_path, toml)


def test_allows_sibling_dests(tmp_path: Path):
    toml = (
        '[[lambda.source]]\nname="a"\ntype="https"\nurl="https://e.com/a"\nextract="none"\ndest="bin/a"\n'
        '[[lambda.source]]\nname="b"\ntype="https"\nurl="https://e.com/b"\nextract="none"\ndest="bin/b"\n'
    )
    assert len(_load(tmp_path, toml)) == 2
