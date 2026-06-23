variable "env" {
  description = "Deployment environment. Prefixes the artifact bucket name (e.g. dev-my-lambda-artifacts)."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for the provider and the deployed Lambda function."
  type        = string
  default     = "eu-west-1"
}
