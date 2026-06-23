output "lambda_function_arn" {
  description = "ARN of the deployed Lambda function."
  value       = module.lambda_app.lambda_function_arn
}

output "lambda_function_name" {
  description = "Name of the deployed Lambda function."
  value       = module.lambda_app.lambda_function_name
}
