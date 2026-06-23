# The Lambda zip referenced below was built and uploaded to S3 by repro-lambda
# OUTSIDE of Terraform (see ../../SETUP.md and the README in this directory).
# Terraform never builds the artifact - it only references the existing object
# in S3 by its content-hash key. The current sha for each lambda is read from
# builds/catalog.json, which repro-lambda updates on every successful build.

locals {
  catalog = jsondecode(file("${path.module}/builds/catalog.json"))
}

module "lambda_app" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${var.env}-app"
  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "app.lambda_handler"
  publish       = true

  s3_existing_package = {
    bucket = "${var.env}-my-lambda-artifacts"
    key    = "lambdas/app/${local.catalog.lambdas.app.current}.zip"
  }
}
