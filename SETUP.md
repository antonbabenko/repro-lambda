# Setting up `repro-lambda` for your project

`repro-lambda` builds and uploads Lambda artifacts to S3 outside of Terraform.
For Terraform to read those artifacts via `s3_existing_package`, you need to
provision the supporting AWS infrastructure once per AWS account and region.

This guide is a copy-paste-able reference. Adapt the names and account IDs to
your environment.

## Architecture

For each environment (e.g. `dev`, `prod`) you typically need:

```
${env}-my-lambda-artifacts            S3 bucket, region = eu-west-1 (or your primary region)
${env}-my-lambda-artifacts-us-east-1  S3 bucket, region = us-east-1 (for Lambda@Edge — optional)

gha-lambda-builder-${env}             IAM role, assumed via GitHub OIDC by source-repo CI to upload
```

Both buckets enforce **key-level immutability** via bucket policy:

- `s3:PutObject` denied unless `If-None-Match=*` is set (writes are first-write-wins)
- `s3:DeleteObject` and `s3:DeleteObjectVersion` denied
- The Lambda service is allowed `s3:GetObject` so functions can load their zip

Once an artifact with a content-hash key (`lambdas/<name>/<sha256>.zip`) is
uploaded, the key is permanently bound to those bytes. `repro-lambda` treats
HTTP 412 PreconditionFailed on a duplicate upload as success.

## Terraform — per-account bootstrap

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
  # Pattern: repo:<owner>/<repo>:* — narrow to specific refs in production.
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

# us-east-1 bucket — only needed for Lambda@Edge. Remove if not using L@E.
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

> Note: if you only use a single region (no Lambda@Edge), delete the
> `lambda_artifacts_bucket_us_east_1` module and the `us_east_1` provider alias.
> The OIDC role policy only needs the primary bucket then.

Apply the Terraform once per environment. The bucket names and role ARN are
referenced by your source repos' CI workflows below.

## GitHub OIDC provider

If your account doesn't already have the GitHub Actions OIDC provider, the
`terraform-aws-modules/iam` collection includes a module for it:

```hcl
module "iam_github_oidc_provider" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-oidc-provider"
  version = "~> 6.0"
}
```

The provider is shared per AWS account — declare it once, not per environment.

## Source-repo CI workflow

In each repo that builds a Lambda, add a thin caller workflow that delegates to
the reusable workflow in `antonbabenko/repro-lambda`:

```yaml
# .github/workflows/build-lambdas.yml
name: build-lambdas

on:
  pull_request:
  push:
    branches: [master]

jobs:
  build:
    uses: antonbabenko/repro-lambda/.github/workflows/build.yml@v0
    with:
      manifest_path: lambdas.toml
      aws_dev_role_arn: arn:aws:iam::<dev-account-id>:role/gha-lambda-builder-dev
      dev_bucket: <env>-lambda-artifacts
      # Optional - master-push upload to a prod bucket:
      # aws_prod_role_arn: arn:aws:iam::<prod-account-id>:role/gha-lambda-builder-prod
      # prod_bucket: <env>-lambda-artifacts
```

Pin the reusable workflow with the sliding major tag `@v0` - it auto-moves to the
latest backward-compatible 0.x release on every tag (switch to `@v1` once repro-lambda
ships 1.0). The role ARNs are not secrets: the boundary is the OIDC trust policy plus
the key-level bucket immutability, so they are plain inputs (only the account IDs,
which are public), not stored secrets.

## Per-Lambda manifest

Each consumer repo defines a `lambdas.toml` at its root:

```toml
[[lambda]]
logical_name      = "app"
source_dir        = "src/app"
requirements_lock = "src/app/requirements.${arch}.lock"
runtime           = "python3.13"
arch              = "arm64"
handler           = "app.lambda_handler"
region            = "eu-west-1"
package_manager   = "pip"
lambda_at_edge    = false
hash_extra        = ""

[builder]
base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:<pinned-digest>"
include_patterns  = ["**/*.py", "**/*.json"]
exclude_patterns  = [".venv/**", "__pycache__/**", "*.pyc", ".git/**", ".env*"]
```

Pin the `base_image_python` to a specific digest with `docker pull <image> &&
docker inspect --format='{{index .RepoDigests 0}}' <image>` — never use a
floating tag in production.

### Per-lambda builder overrides

`[builder]` sets the defaults for every lambda. Any `[[lambda]]` may override
`base_image_python`, `include_patterns`, or `exclude_patterns` for itself. An
override REPLACES the default for that field (lists are not merged); an unset
field inherits `[builder]`. Use this when one lambda needs a different base image
or a tighter file filter (so it re-hashes only on changes that affect it):

```toml
[[lambda]]
logical_name      = "worker"
source_dir        = "src/worker"
requirements_lock = "src/worker/requirements.${arch}.lock"
runtime           = "python3.13"
arch              = "arm64"
handler           = "worker.handler"
# Override: only this lambda's runtime modules trigger a rebuild, and it builds
# on its own pinned base image. base_image_python must still be digest-pinned.
base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:<other-digest>"
include_patterns  = ["worker/**/*.py", "worker/**/*.json"]
exclude_patterns  = ["**/tests/**"]
```

The resolved per-lambda builder (base-image digest + include/exclude lists +
builder version) folds into the content hash, so changing an override re-keys
that lambda's artifact while leaving the others untouched.

## Declarative sources — `[[lambda.source]]`

A lambda can bundle pinned external artifacts (a release tarball, a vendored CLI
binary) fetched at build time, instead of a hand-rolled download script. Each
`[[lambda.source]]` is fully pinned and fetched + verified + extracted into the
package before the container build:

```toml
[[lambda]]
logical_name      = "app"
source_dir        = "src/app"
requirements_lock = "src/app/requirements.${arch}.lock"
runtime           = "python3.13"
arch              = "arm64"
handler           = "app.lambda_handler"

# A private GitHub release tarball -> staged at package path "vendor".
[[lambda.source]]
name    = "vendor"
type    = "github_release"
repo    = "owner/vendor-tool"
tag     = "vendor-v{version}"
asset   = "vendor-{version}.tar.gz"
sha256  = "<64-hex; written by `repro-lambda lock`>"
extract = "tar.gz"
member  = "vendor-{version}"   # map the versioned top dir to dest
dest    = "vendor"
version = "1.4.0"              # bump this one line; lock re-pins everything

# A public binary whose version is derived from the vendor release's .tool-versions.
[[lambda.source]]
name    = "terraform"
type    = "https"
url     = "https://releases.hashicorp.com/terraform/{version}/terraform_{version}_linux_arm64.zip"
sha256  = "<64-hex; written by lock>"
extract = "zip"
member  = "terraform"
dest    = "bin/terraform"
executable = true
version = "1.9.0"
[lambda.source.version_from]
source = "vendor"          # read from the vendor source's extracted tree
file   = ".tool-versions"  # relative to its member-stripped root
key    = "terraform"
```

- **Pinning.** `sha256` is verified before the archive is opened. `extract` is
  `zip` / `tar.gz` / `none`. `member` extracts one file or a directory subtree to
  `dest`; omit it to extract the whole archive under `dest`. Source names are
  unique per lambda and dests may not overlap each other or the staged source.
- **`version_from`** (single-level) derives a source's `version` from an asdf-style
  `key value` line in another source's file, so bumping the root `version`
  cascades to dependents. It is a lock input - it never affects the artifact hash.
- **`repro-lambda lock`** re-resolves `version_from`, re-downloads, recomputes each
  `sha256`, and rewrites this file (comment-preserving, atomic, idempotent). Run it
  after bumping a `version`. Pass `REPRO_LAMBDA_SOURCES_TOKEN` for private
  `github_release` sources.
- **Security.** Fetches are HTTPS-only with an SSRF guard (no private/loopback/
  link-local/metadata IPs), strip `Authorization` on cross-host redirects, verify
  sha256 before extraction, and reject path-traversal / link / device entries with
  decompression-bomb bounds. See `src/repro_lambda/sources.py`.

In CI, pass the token to the reusable `build.yml` as the `sources_token` secret:

```yaml
jobs:
  build:
    uses: antonbabenko/repro-lambda/.github/workflows/build.yml@v0
    with:
      manifest_path: lambdas.toml
      aws_dev_role_arn: arn:aws:iam::<account>:role/<dev-builder-role>
      dev_bucket: <env>-my-lambda-artifacts
    secrets:
      sources_token: ${{ secrets.MY_RELEASE_TOKEN }}
```

## Terraform consumer — `s3_existing_package`

In the Terraform that creates your Lambda function, point at the artifact in
S3 instead of building inline:

```hcl
locals {
  lambda_manifest = jsondecode(file("${path.module}/builds/catalog.json"))
}

module "lambda_app" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "my-app"
  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "app.lambda_handler"
  publish       = true

  s3_existing_package = {
    bucket         = "${var.env}-my-lambda-artifacts"
    key            = "lambdas/app/${local.lambda_manifest.lambdas.app.current}.zip"
    object_version = null
  }
}
```

For Lambda@Edge, point at the `-us-east-1` bucket and set `lambda_at_edge = true`
in the Terraform module.

## Smoke test

After applying the bootstrap Terraform and configuring CI secrets:

```bash
# In your consumer repo
gh workflow run build-lambdas.yml
# Wait for completion, then check that the artifact landed
aws s3 ls s3://dev-my-lambda-artifacts/lambdas/app/
```

The first PR after migration should show a Terraform plan whose only diff is the
`s3_key` change. If you see `last_modified`, `qualified_arn`, `version`,
`local_file.archive_plan`, or `null_resource.archive` mentioned, the migration
is incomplete — review your Lambda module call and remove the legacy build
attributes (`source_path`, `build_in_docker`, `trigger_on_package_timestamp`,
`ignore_source_code_hash`, `hash_extra`, `local_existing_package`,
`store_on_s3`).

## Troubleshooting

- **`403 AccessDenied` on upload**: the OIDC role doesn't have `s3:PutObject` on
  the bucket. Check the inline policy resources include both the bucket ARN and
  `${bucket_arn}/*`.
- **`PreconditionFailed` on every upload**: the artifact already exists with
  that sha. This is the expected cache-hit behavior; `repro-lambda` treats it
  as success.
- **AWS CLI multipart upload fails with 403**: bucket policy denies multipart
  parts. Pin the multipart threshold above your Lambda size cap:
  `aws configure set s3.multipart_threshold 300MB`.
- **Plan still shows noisy diff after migration**: verify the consumer Terraform
  removed every legacy attribute and that the catalog sha read by `jsondecode`
  matches the sha in the S3 key. The plan diff should be exactly `~ s3_key =
  "<old>.zip" -> "<new>.zip"` and nothing else.

## Node.js (npm) Lambdas

`repro-lambda` v0.2 adds Node.js Lambda packaging. The build runs `npm ci` in
the digest-pinned Node base image, then packs the resulting `pkg/` directory
inside the digest-pinned Python base image (Python is the only language with
deterministic-zip tooling pre-installed in the AWS Lambda runtime images, so
its zlib is the only deflate implementation invoked - macOS arm64 hosts and
Linux x86_64 CI produce byte-identical output).

### Manifest fields for npm specs

```toml
[[lambda]]
logical_name      = "api"
source_dir        = "src/api"
requirements_lock = "src/api/package-lock.json"   # npm lockfile
package_json      = "src/api/package.json"        # REQUIRED for npm specs
runtime           = "nodejs22.x"                  # or "nodejs20.x"
arch              = "x86_64"                      # or "arm64"
handler           = "index.handler"
region            = "eu-west-1"
package_manager   = "npm"
lambda_at_edge    = false
hash_extra        = ""

[builder]
base_image_python = "public.ecr.aws/lambda/python:3.13@sha256:<pinned-digest>"
base_image_nodejs = "public.ecr.aws/lambda/nodejs:22@sha256:<pinned-digest>"
include_patterns  = ["**/*.js", "**/*.json"]
exclude_patterns  = [".git/**", "node_modules/**", "*.md", "LICENSE*", "CHANGELOG*"]
```

Pin both base images by digest:

```bash
docker pull public.ecr.aws/lambda/nodejs:22
docker inspect --format='{{index .RepoDigests 0}}' public.ecr.aws/lambda/nodejs:22
```

### Lockfile regeneration

`repro-lambda lock` only regenerates per-arch Python lockfiles via `uv pip
compile`. For npm specs it prints `skip <name>: npm uses package-lock.json
directly`. Regenerate the npm lockfile upstream with:

```bash
cd src/api
npm install --package-lock-only
git add package-lock.json
```

The lockfile and `package.json` both contribute to the artifact content hash;
editing either bumps the S3 key.

## Lambda@Edge example

Lambda@Edge functions must be deployed to `us-east-1`, regardless of where the
CloudFront distribution serves traffic. Set `region = "us-east-1"` and
`lambda_at_edge = true` in the manifest; `repro-lambda` will upload to the
`*-us-east-1` artifact bucket automatically.

```toml
[[lambda]]
logical_name      = "edge"
source_dir        = "src/edge"
requirements_lock = "src/edge/package-lock.json"
package_json      = "src/edge/package.json"
runtime           = "nodejs22.x"
arch              = "x86_64"               # L@E currently requires x86_64
handler           = "index.handler"
region            = "us-east-1"
package_manager   = "npm"
lambda_at_edge    = true
```

Consumer Terraform points at the us-east-1 bucket:

```hcl
module "lambda_edge" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"
  providers = { aws = aws.us_east_1 }

  function_name  = "my-edge"
  runtime        = "nodejs22.x"
  architectures  = ["x86_64"]
  handler        = "index.handler"
  publish        = true
  lambda_at_edge = true

  s3_existing_package = {
    bucket = "${var.env}-my-lambda-artifacts-us-east-1"
    key    = "lambdas/edge/${local.lambda_manifest.lambdas.edge.current}.zip"
  }
}
```

## Caveats

- **No npm workspaces.** v0.2 supports a single `package.json` per Lambda. If
  your repo uses workspaces, copy the published package into a single-package
  layout per Lambda before invoking `repro-lambda`.
- **Native dependencies need `optionalDependencies` arms.** `npm ci --cpu=${arch}
  --os=linux` cannot cross-compile native modules. A dep with native code must
  ship a `linux-${arch}` binary via its `package-lock.json`
  `optionalDependencies` arm (the lockfile-v3 standard mechanism). If it
  doesn't, the build runs on the host arch and may produce a non-portable
  artifact.
- **Symlinks in source are skipped.** `pack_directory` skips symlinks and
  prints a stderr warning; the resulting zip cannot preserve link semantics.
  If your build relies on symlinks (e.g. monorepo references), replace them
  with the file contents.
