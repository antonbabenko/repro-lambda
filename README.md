# repro-lambda

Build reproducible AWS Lambda packages outside Terraform, optimized for
[terraform-aws-lambda](https://registry.terraform.io/modules/terraform-aws-modules/lambda/aws/latest) by [serverless.tf](https://serverless.tf/).

Produces byte-identical zip files across local dev (macOS) and CI (Linux),
uploads to S3 by content-hash key, and lets Terraform read `s3_existing_package`
instead of building during `terraform plan`/`apply`.

## Install

    pip install repro-lambda

## Usage

    repro-lambda init           # scaffold lambdas.toml and CI caller workflow
    repro-lambda lock           # regenerate per-arch lockfiles
    repro-lambda build          # build all lambdas in lambdas.toml, upload to S3
    repro-lambda build --verify # two-pass byte-reproducibility check

See `docs/` for full design.

## Release

Releases are tag-driven. To cut v0.1.1:

    git tag v0.1.1
    git push origin v0.1.1

The `publish.yml` workflow uses PyPI Trusted Publishing (OIDC) — no PyPI token
needed in repo secrets. Configure once via PyPI's "Publishing" panel:

- Owner: `antonbabenko`
- Repository: `repro-lambda`
- Workflow: `publish.yml`
- Environment: (leave blank)

## Setup

See [SETUP.md](./SETUP.md) for a copy-paste-able guide to provisioning the
S3 buckets, IAM OIDC role, and CI workflow needed to use `repro-lambda` in
your project.
