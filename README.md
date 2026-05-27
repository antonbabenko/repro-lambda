# repro-lambda

Build reproducible AWS Lambda packages outside Terraform, optimized for
[terraform-aws-lambda by serverless.tf](https://registry.terraform.io/modules/terraform-aws-modules/lambda/aws/latest).

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
