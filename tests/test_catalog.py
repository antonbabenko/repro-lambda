import json
from pathlib import Path

from repro_lambda.catalog import Catalog, CatalogEntry, load_catalog


def test_load_catalog_missing_file_returns_empty(tmp_path: Path):
    cat = load_catalog(tmp_path / "catalog.json")
    assert cat == Catalog(lambdas={})


def test_save_then_load_round_trip(tmp_path: Path):
    cat = Catalog(lambdas={})
    cat.record(
        "app",
        CatalogEntry(
            sha256="abc",
            source_commit="deadbeef",
            runtime="python3.13",
            arch="arm64",
            region="eu-west-1",
            builder_version="0.1.0",
            base_image_digest="sha256:0",
            built_at="2026-05-27T09:30:00Z",
        ),
    )
    path = tmp_path / "catalog.json"
    cat.save(path)
    loaded = load_catalog(path)
    assert loaded == cat
    assert loaded.lambdas["app"].current == "abc"


def test_history_caps_at_10_entries(tmp_path: Path):
    cat = Catalog(lambdas={})
    for i in range(15):
        cat.record(
            "app",
            CatalogEntry(
                sha256=f"sha{i:02d}",
                source_commit=f"commit{i}",
                runtime="python3.13",
                arch="arm64",
                region="eu-west-1",
                builder_version="0.1.0",
                base_image_digest="sha256:0",
                built_at=f"2026-05-{i + 1:02d}T00:00:00Z",
            ),
        )
    assert cat.lambdas["app"].current == "sha14"
    assert len(cat.lambdas["app"].history) == 10
    assert [e.sha256 for e in cat.lambdas["app"].history] == [
        f"sha{i:02d}" for i in range(14, 4, -1)
    ]


def test_record_dedup_keeps_existing_entry_when_sha_unchanged():
    cat = Catalog(lambdas={})
    e1 = CatalogEntry(
        sha256="abc",
        source_commit="c1",
        runtime="python3.13",
        arch="arm64",
        region="eu-west-1",
        builder_version="0.1.0",
        base_image_digest="sha256:0",
        built_at="2026-05-27T00:00:00Z",
    )
    cat.record("app", e1)
    e2 = CatalogEntry(**{**e1.__dict__, "built_at": "2026-05-27T01:00:00Z"})
    cat.record("app", e2)
    assert cat.lambdas["app"].current == "abc"
    assert len(cat.lambdas["app"].history) == 1


def test_save_writes_valid_json_schema(tmp_path: Path):
    cat = Catalog(lambdas={})
    cat.record(
        "app",
        CatalogEntry(
            sha256="abc",
            source_commit="c",
            runtime="python3.13",
            arch="arm64",
            region="eu-west-1",
            builder_version="0.1.0",
            base_image_digest="sha256:0",
            built_at="2026-05-27T00:00:00Z",
        ),
    )
    path = tmp_path / "catalog.json"
    cat.save(path)
    raw = json.loads(path.read_text())
    assert raw["schema_version"] == 1
    assert "lambdas" in raw
    assert raw["lambdas"]["app"]["current"] == "abc"
