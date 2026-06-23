# repro-lambda

Build reproducible AWS Lambda packages outside Terraform, optimized for
[terraform-aws-lambda](https://registry.terraform.io/modules/terraform-aws-modules/lambda/aws/latest) by [serverless.tf](https://serverless.tf/).

Produces byte-identical zip files across local dev (macOS) and CI (Linux),
uploads to S3 by content-hash key, and lets Terraform read `s3_existing_package`
instead of building during `terraform plan`/`apply`.

## Install

    pip install repro-lambda

## Quick start

    repro-lambda lock                     # regenerate per-arch requirement + source locks
    repro-lambda build --bucket <bucket>  # build all lambdas in lambdas.toml, upload to S3
    repro-lambda build --verify --dry-run # two-pass byte-reproducibility check (no upload)
    repro-lambda promote \
      --dev-bucket <dev> --prod-bucket <prod>  # copy dev -> prod by content sha (no rebuild)

`--bucket` (or `REPRO_LAMBDA_BUCKET`) is required for a real build; add
`--dry-run` to build without uploading. There is no `init` command yet (the
subcommand is currently a stub).

## Documentation

What each document covers, by section:

### Setup - one-time AWS provisioning ([SETUP.md](./SETUP.md))

Provision the supporting infrastructure once per AWS account and environment.

- [Architecture](./SETUP.md#architecture) - artifact buckets, key-level immutability, the content-hash model
- [Terraform - per-account bootstrap](./SETUP.md#terraform---per-account-bootstrap) - the buckets, the GitHub OIDC builder role, and outputs
- [GitHub OIDC provider](./SETUP.md#github-oidc-provider) - declaring the shared per-account OIDC provider
- [Next steps](./SETUP.md#next-steps) - where to go after provisioning

### Usage - day-to-day ([USAGE.md](./USAGE.md))

Using `repro-lambda` once the infrastructure exists.

- [Source-repo CI workflow](./USAGE.md#source-repo-ci-workflow) - wiring the reusable build workflow into CI/CD
- [Per-Lambda manifest](./USAGE.md#per-lambda-manifest) - the `lambdas.toml` fields
  - [Per-lambda builder overrides](./USAGE.md#per-lambda-builder-overrides) - per-lambda base image and file filters
- [Declarative sources](./USAGE.md#declarative-sources---lambdasource) - pinned external artifacts via `[[lambda.source]]`
- [Terraform consumer (`s3_existing_package`)](./USAGE.md#terraform-consumer---s3_existing_package) - wiring `terraform-aws-modules/lambda/aws` to the built artifact
- [Smoke test](./USAGE.md#smoke-test) - first-build verification and the clean migration plan diff
- [Troubleshooting](./USAGE.md#troubleshooting) - upload 403s, `PreconditionFailed`, noisy plans
- [Node.js (npm) Lambdas](./USAGE.md#nodejs-npm-lambdas) - npm packaging support
  - [Manifest fields for npm specs](./USAGE.md#manifest-fields-for-npm-specs)
  - [Lockfile regeneration](./USAGE.md#lockfile-regeneration)
- [Lambda@Edge example](./USAGE.md#lambdaedge-example) - `us-east-1` artifacts for CloudFront
- [Caveats](./USAGE.md#caveats) - npm workspaces, native deps, symlinks

### Example - runnable ([examples/complete/](./examples/complete/))

A self-contained consumer setup: manifest, catalog, and Terraform using
`terraform-aws-modules/lambda/aws`.

- [What this example shows](./examples/complete/README.md#what-this-example-shows) - files and layout
- [The build-outside-Terraform flow](./examples/complete/README.md#the-build-outside-terraform-flow) - build, inspect the catalog, apply
- [Expected plan diff](./examples/complete/README.md#expected-plan-diff) - the `s3_key`-only diff to expect
