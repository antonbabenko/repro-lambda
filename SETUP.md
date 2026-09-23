# Setting up `repro-lambda` for your project

`repro-lambda` builds and uploads Lambda artifacts to S3 outside of Terraform.
This guide covers the one-time job: provisioning the supporting AWS
infrastructure (the artifact buckets plus the GitHub OIDC builder role) once per
AWS account and region, so that Terraform can later read those artifacts via
`s3_existing_package`.

This is a copy-paste-able reference. Adapt the names and account IDs to your
environment. Once the infrastructure exists, see [USAGE.md](./USAGE.md) for
day-to-day use (the CI workflow, the per-Lambda manifest, and consuming
artifacts from Terraform).

## Architecture

For each environment (e.g. `dev`, `prod`) you typically need:

```
${env}-my-lambda-artifacts            S3 bucket, region = eu-west-1 (or your primary region)
${env}-my-lambda-artifacts-us-east-1  S3 bucket, region = us-east-1 (for Lambda@Edge - optional)

gha-lambda-builder-${env}             IAM role, assumed via GitHub OIDC by source-repo CI to upload
```

Both buckets enforce **key-level immutability** via bucket policy:

- `s3:PutObject` denied unless `If-None-Match=*` is set (writes are first-write-wins)
- `s3:DeleteObject` and `s3:DeleteObjectVersion` denied
- The Lambda service is allowed `s3:GetObject` so functions can load their zip

Once an artifact with a content-hash key (`lambdas/<name>/<sha256>.zip`) is
uploaded, the key is permanently bound to those bytes. `repro-lambda` treats
HTTP 412 PreconditionFailed on a duplicate upload as success.

The key hashes the build inputs (staged source, requirements lock, spec, base
image, filters, sources) plus a builder hash contract. It does not hash the
repro-lambda package version, so upgrading repro-lambda keeps every existing key
unless the release changes the packaged bytes, in which case it bumps
`HASH_CONTRACT` in `hasher.py` and says so in its changelog entry.
`tests/test_hash_contract.py` fingerprints the packaging modules, so a change to
them fails CI until the contract is reviewed.

`--verify` builds twice and compares the two results; it does not compare against
the object already in S3. On a cache hit the build catalog records
`hash-contract:<value>` as the builder, because the running version did not
build that object.

Keys changed once, in 0.8.2: releases up to 0.8.1 folded the package version, so
0.8.1 minted keys of its own. 0.8.2 returns to the keys 0.8.0 minted. A pin taken
from a 0.8.1 build moves back on the next build, with identical bytes.

## Terraform - per-account bootstrap

The Terraform below assumes you already have a configured AWS provider in the
target account. Drop it into your bootstrap directory (or anywhere you provision
account-wide infrastructure that has no dependencies on other Terraform state).

```hcl
# us-east-1 is needed only for Lambda@Edge artifacts. Skip if you don't use L@E.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

locals {
  env                     = "dev" # change per environment
  lambda_artifacts_bucket_primary  = "${local.env}-my-lambda-artifacts"
  lambda_artifacts_bucket_us_east_1 = "${local.env}-my-lambda-artifacts-us-east-1"

  # GitHub OIDC subjects allowed to assume the builder role.
  # Pattern: repo:<owner>/<repo>:* - narrow to specific refs in production.
  lambda_builder_oidc_subjects = [
    "repo:my-org/my-source-repo:*",
  ]
}

# Immutability policy applied to every artifact bucket.
data "aws_iam_policy_document" "lambda_artifacts_immutability" {
  for_each = toset([
    local.lambda_artifacts_bucket_primary,
    local.lambda_artifacts_bucket_us_east_1,
  ])

  statement {
    sid    = "DenyOverwrites"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${each.value}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:If-None-Match"
      values   = ["*"]
    }
  }

  statement {
    sid    = "DenyDelete"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["arn:aws:s3:::${each.value}/*"]
  }

  statement {
    sid    = "AllowLambdaServiceRead"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${each.value}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_caller_identity" "current" {}

# Primary-region bucket
module "lambda_artifacts_bucket_primary" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket = local.lambda_artifacts_bucket_primary

  versioning = {
    enabled = false # sha-as-key IS the versioning
  }

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  attach_policy = true
  policy        = data.aws_iam_policy_document.lambda_artifacts_immutability[local.lambda_artifacts_bucket_primary].json

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  force_destroy = false
}

# us-east-1 bucket - only needed for Lambda@Edge. Remove if not using L@E.
module "lambda_artifacts_bucket_us_east_1" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  providers = {
    aws = aws.us_east_1
  }

  bucket = local.lambda_artifacts_bucket_us_east_1

  versioning = {
    enabled = false
  }

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  attach_policy = true
  policy        = data.aws_iam_policy_document.lambda_artifacts_immutability[local.lambda_artifacts_bucket_us_east_1].json

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  force_destroy = false
}

# OIDC role assumed by GitHub Actions in source repos to upload artifacts.
module "gha_lambda_builder_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role"
  version = "~> 6.0"

  name = "gha-lambda-builder-${local.env}"

  enable_github_oidc     = true
  oidc_wildcard_subjects = local.lambda_builder_oidc_subjects

  create_inline_policy = true
  inline_policy_permissions = {
    WriteLambdaArtifacts = {
      actions = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:HeadObject",
        "s3:ListBucket",
      ]
      resources = [
        module.lambda_artifacts_bucket_primary.s3_bucket_arn,
        "${module.lambda_artifacts_bucket_primary.s3_bucket_arn}/*",
        module.lambda_artifacts_bucket_us_east_1.s3_bucket_arn,
        "${module.lambda_artifacts_bucket_us_east_1.s3_bucket_arn}/*",
      ]
    }
  }
}

output "lambda_artifacts_bucket_primary_arn" {
  value = module.lambda_artifacts_bucket_primary.s3_bucket_arn
}

output "lambda_artifacts_bucket_us_east_1_arn" {
  value = module.lambda_artifacts_bucket_us_east_1.s3_bucket_arn
}

output "gha_lambda_builder_role_arn" {
  value = module.gha_lambda_builder_role.arn
}
```

> Note: if you only use a single region (no Lambda@Edge), remove every
> us-east-1 reference together, or `terraform plan` fails on dangling
> references: the `lambda_artifacts_bucket_us_east_1` module, the `us_east_1`
> provider alias, the `local.lambda_artifacts_bucket_us_east_1` entry in the
> immutability policy `for_each`, and the two
> `module.lambda_artifacts_bucket_us_east_1` ARNs in the OIDC role's
> `WriteLambdaArtifacts` resources.

Apply the Terraform once per environment. The bucket names and role ARN are
referenced by your source repos' CI workflows in [USAGE.md](./USAGE.md).

## GitHub OIDC provider

If your account doesn't already have the GitHub Actions OIDC provider, the
`terraform-aws-modules/iam` collection includes a module for it:

```hcl
module "iam_github_oidc_provider" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-oidc-provider"
  version = "~> 6.0"
}
```

The provider is shared per AWS account - declare it once, not per environment.

## Next steps

The AWS infrastructure is now provisioned. To use it day-to-day:

- [USAGE.md](./USAGE.md) - the source-repo CI workflow, the per-Lambda manifest,
  declarative sources, consuming artifacts from Terraform, and troubleshooting.
- [examples/complete/](./examples/complete/) - a runnable example that wires the
  pieces together.
