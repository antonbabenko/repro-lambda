from pathlib import Path

import pytest

from repro_lambda.manifest import LambdaSpec, Manifest, load_manifest


def test_load_manifest_parses_single_lambda(fixtures_dir: Path):
    manifest = load_manifest(fixtures_dir / "sample_python_lambda" / "lambdas.toml")
    assert isinstance(manifest, Manifest)
    assert len(manifest.lambdas) == 1
    spec = manifest.lambdas[0]
    assert isinstance(spec, LambdaSpec)
    assert spec.logical_name == "app"
    assert spec.source_dir == "handler"
    assert spec.runtime == "python3.13"
    assert spec.arch == "arm64"
    assert spec.handler == "app.lambda_handler"
    assert spec.region == "eu-west-1"
    assert spec.package_manager == "pip"
    assert spec.lambda_at_edge is False


def test_lambda_spec_resolves_requirements_lock_template(fixtures_dir: Path):
    manifest = load_manifest(fixtures_dir / "sample_python_lambda" / "lambdas.toml")
    spec = manifest.lambdas[0]
    assert spec.resolved_requirements_lock == "handler/requirements.arm64.lock"


def test_load_manifest_rejects_unknown_runtime(tmp_path: Path):
    bad = tmp_path / "lambdas.toml"
    bad.write_text(
        '[[lambda]]\n'
        'logical_name = "x"\n'
        'source_dir = "x"\n'
        'requirements_lock = "x.lock"\n'
        'runtime = "rust1.79"\n'
        'arch = "arm64"\n'
        'handler = "x.h"\n'
        '\n'
        '[builder]\n'
        'base_image_python = "x@sha256:0"\n'
    )
    with pytest.raises(ValueError, match="unsupported runtime"):
        load_manifest(bad)


def test_load_manifest_rejects_unknown_arch(tmp_path: Path):
    bad = tmp_path / "lambdas.toml"
    bad.write_text(
        '[[lambda]]\n'
        'logical_name = "x"\n'
        'source_dir = "x"\n'
        'requirements_lock = "x.lock"\n'
        'runtime = "python3.13"\n'
        'arch = "mips"\n'
        'handler = "x.h"\n'
        '\n'
        '[builder]\n'
        'base_image_python = "x@sha256:0"\n'
    )
    with pytest.raises(ValueError, match="unsupported arch"):
        load_manifest(bad)


def test_load_manifest_rejects_unpinned_base_image(tmp_path: Path):
    bad = tmp_path / "lambdas.toml"
    bad.write_text(
        '[[lambda]]\n'
        'logical_name = "x"\n'
        'source_dir = "x"\n'
        'requirements_lock = "x.lock"\n'
        'runtime = "python3.13"\n'
        'arch = "arm64"\n'
        'handler = "x.h"\n'
        '\n'
        '[builder]\n'
        'base_image_python = "public.ecr.aws/lambda/python:3.13"\n'
    )
    with pytest.raises(ValueError, match="must be pinned by digest"):
        load_manifest(bad)
