"""Pin v0.1 Python zip output bytes -- v0.2 must reproduce them byte-for-byte.

OPERATOR PROCEDURE TO RECORD REFERENCE:
1. docker pull public.ecr.aws/lambda/python:3.13
2. docker inspect --format='{{index .RepoDigests 0}}' public.ecr.aws/lambda/python:3.13
   -> copy the full image@sha256:<digest> string
3. git worktree add /tmp/repro-lambda-v01 v0.1.0
4. cp tests/test_python_byte_compat_regression.py /tmp/repro-lambda-v01/tests/
5. cd /tmp/repro-lambda-v01
6. Edit PINNED_PYTHON_IMAGE in /tmp/repro-lambda-v01/tests/test_python_byte_compat_regression.py
   with the digest from step 2. Leave V0_1_REFERENCE_SHA empty.
7. uv run pytest tests/test_python_byte_compat_regression.py -v
8. Test fails with "expected: ; got: <SHA>". Copy that SHA.
9. cd back to main worktree. Edit V0_1_REFERENCE_SHA + PINNED_PYTHON_IMAGE in the
   committed test with the captured values.
10. Commit: "test(regression): record v0.1.0 Python byte-output reference sha"
11. git worktree remove /tmp/repro-lambda-v01

The test fails-hard until both V0_1_REFERENCE_SHA and PINNED_PYTHON_IMAGE are recorded;
there is no pytest.skip escape hatch. This is intentional: a silently-skipped
regression is a non-regression.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from repro_lambda.docker_runner import build_python_lambda
from repro_lambda.manifest import BuilderConfig
from repro_lambda.source_stager import stage_source

# The fixture's lockfile basename.
FIXTURE_LOCK_FILENAME = "requirements.arm64.lock"

# Reference sha recorded by running this test on the v0.1.0 release tag against
# the digest-pinned base image. The digest is part of the test contract: if you
# bump the base image digest, you MUST re-record this value with an explicit
# audit comment in the commit that bumps both.
V0_1_REFERENCE_SHA = ""  # paste captured sha here (see operator procedure above)

# The pinned base image to record against. Must be `image@sha256:<64-hex>` form.
# Recorded once at v0.1->v0.2 cut-line; changing this digest invalidates the reference.
PINNED_PYTHON_IMAGE = ""  # paste public.ecr.aws/lambda/python:3.13@sha256:<digest> here


def _is_recorded() -> bool:
    """Both placeholders must be non-empty and PINNED_PYTHON_IMAGE must be a digest-pin."""
    if not V0_1_REFERENCE_SHA:
        return False
    if "@sha256:" not in PINNED_PYTHON_IMAGE:
        return False
    # Reject placeholders like "image@sha256:" with no digest suffix.
    return not PINNED_PYTHON_IMAGE.endswith("@sha256:")


@pytest.mark.docker
def test_python_lambda_byte_identical_to_v01_reference(tmp_path: Path) -> None:
    """Build the sample_python_lambda fixture; sha must equal the recorded v0.1 reference."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if not _is_recorded():
        pytest.fail(
            "V0_1_REFERENCE_SHA + PINNED_PYTHON_IMAGE must be recorded before this test can run. "
            "See the module docstring's OPERATOR PROCEDURE for the steps."
        )

    fixture = Path(__file__).parent / "fixtures" / "sample_python_lambda"
    repo = tmp_path / "repo"
    shutil.copytree(fixture, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    builder = BuilderConfig(
        base_image_python=PINNED_PYTHON_IMAGE,
        include_patterns=["**/*.py"],
        exclude_patterns=[".git/**"],
    )
    stage = tmp_path / "stage"
    lock = repo / "handler" / FIXTURE_LOCK_FILENAME
    assert lock.exists(), f"fixture lockfile missing: {lock}"
    stage_source(
        repo_root=repo,
        source_dir="handler",
        builder=builder,
        stage_dir=stage,
        extra_files=[(lock, "requirements.lock")],
    )
    out = stage / "lambda.zip"
    build_python_lambda(
        stage_dir=stage,
        out_zip=out,
        base_image=builder.base_image_python,
        arch="arm64",
        python_version="3.13",
    )

    actual_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    assert actual_sha == V0_1_REFERENCE_SHA, (
        f"v0.1 byte-compat broken!\n"
        f"  expected: {V0_1_REFERENCE_SHA}\n"
        f"  got:      {actual_sha}\n"
        f"DO NOT update the reference without an explicit audit of the container script."
    )
