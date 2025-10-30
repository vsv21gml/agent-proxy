

output "lambda_function_name" {
  value = aws_lambda_function.bedrock_proxy.function_name
}

output "elasticache_redis_endpoint" {
  value = aws_elasticache_replication_group.main.primary_endpoint_address
}