# Complete repro-lambda example

A self-contained consumer setup showing how `repro-lambda` and Terraform divide
the work: `repro-lambda` builds a reproducible Lambda zip and uploads it to S3
outside of Terraform, and Terraform only references that existing object by its
content-hash key. No build runs during `terraform plan` or `apply`.

## What this example shows

- One Python Lambda named `app` (`python3.13`, `arm64`, region `eu-west-1`).
- A manifest (`lambdas.toml`) that `repro-lambda` reads to build and upload.
- A build catalog (`builds/catalog.json`) committed to the repo, holding the
  current content sha per lambda.
- Terraform that reads the current sha and points `s3_existing_package` at
  `lambdas/app/<sha256>.zip` in the artifact bucket.

## Files

```
examples/complete/
  README.md                      this file
  lambdas.toml                   repro-lambda manifest (one python lambda: app)
  main.tf                        lambda module call using s3_existing_package
  variables.tf                   env, aws_region
  versions.tf                    required_version + aws provider + provider block
  outputs.tf                     lambda_function_arn, lambda_function_name
  builds/catalog.json            current sha + build history for "app"
  src/app/app.py                 the handler
  src/app/requirements.arm64.lock locked deps (empty - no third-party deps)
```

## The build-outside-Terraform flow

### 1. Build and upload the artifact

`repro-lambda` builds the zip in a digest-pinned container and uploads it to the
artifact bucket. The S3 key is `lambdas/<logical_name>/<sha256>.zip`.

```bash
repro-lambda build --bucket dev-my-lambda-artifacts
```

This also updates `builds/catalog.json` with the new content sha. Commit that
change to the repo so Terraform reads the matching sha.

### 2. Inspect the catalog

Terraform reads the current sha from `builds/catalog.json`:

```bash
# What sha will Terraform deploy?
cat builds/catalog.json
# Confirm the object exists in S3
aws s3 ls s3://dev-my-lambda-artifacts/lambdas/app/
```

### 3. Apply the Terraform

```bash
terraform init
terraform apply -var="env=dev"
```

Terraform reads `local.catalog.lambdas.app.current` and resolves the key
`lambdas/app/<sha256>.zip`. It does not build anything.

## Expected plan diff

When `app.py` (or its deps, base image, or builder version) changes, the next
`repro-lambda build` writes a new sha to `builds/catalog.json`. The only
Terraform plan diff should then be the S3 key:

```
~ s3_key = "lambdas/app/<old-sha>.zip" -> "lambdas/app/<new-sha>.zip"
```

If you also see `last_modified`, `qualified_arn`, `source_code_hash`, or any
`null_resource.archive` / `local_file.archive_plan`, the function is still
configured to build inline - remove the legacy build attributes. See the
migration notes in [../../USAGE.md](../../USAGE.md#smoke-test).

## This example will not apply as-is

It documents the flow; it is not expected to `terraform apply` cleanly out of
the box. Before it can apply you must:

- Provision the artifact bucket and OIDC builder role once per account - see
  [../../SETUP.md](../../SETUP.md).
- Replace the placeholders: `<pinned-digest>` in `lambdas.toml`, and any account
  IDs / bucket names that differ from `${env}-my-lambda-artifacts`.
- Run `repro-lambda build --bucket <your-bucket>` so a real artifact exists in
  S3 at the sha recorded in `builds/catalog.json` (the sha shipped here is a
  placeholder).

## Related docs

- [../../USAGE.md](../../USAGE.md) - day-to-day usage: CI workflow, manifest,
  and the consumer Terraform reference (`s3_existing_package`).
- [../../SETUP.md](../../SETUP.md) - one-time account bootstrap (artifact
  buckets, OIDC builder role).
