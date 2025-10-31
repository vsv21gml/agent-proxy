provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

# 네트워킹 설정 (VPC, Subnets, Security Groups)
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true  # VPC 엔드포인트 사용을 위해 필요
  enable_dns_support   = true  # VPC 엔드포인트 사용을 위해 필요
  
  tags = {
    Name = "bedrock-proxy-vpc"
  }
}

resource "aws_subnet" "private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = "${var.aws_region}a"
  tags = {
    Name = "bedrock-proxy-private-subnet-a"
  }
}

resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = "${var.aws_region}b"
  tags = {
    Name = "bedrock-proxy-private-subnet-b"
  }
}

# VPC 엔드포인트용 보안 그룹
resource "aws_security_group" "vpc_endpoint_sg" {
  name        = "vpc-endpoint-sg"
  description = "Security group for VPC endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda_sg.id]
  }

  tags = {
    Name = "bedrock-proxy-vpc-endpoint-sg"
  }
}

resource "aws_security_group" "lambda_sg" {
  name        = "lambda-sg"
  description = "Security group for Lambda function"
  vpc_id      = aws_vpc.main.id

  # ElastiCache 접근용
  egress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.elasticache_sg.id]
  }

  # VPC 엔드포인트 접근용 (Bedrock)
  egress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.vpc_endpoint_sg.id]
  }

  tags = {
    Name = "bedrock-proxy-lambda-sg"
  }
}

resource "aws_security_group" "elasticache_sg" {
  name        = "elasticache-sg"
  description = "Security group for ElastiCache cluster"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda_sg.id]
  }

  tags = {
    Name = "bedrock-proxy-elasticache-sg"
  }
}

# Bedrock VPC 엔드포인트
resource "aws_vpc_endpoint" "bedrock" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  
  private_dns_enabled = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = "*"
        Action = [
          "bedrock:*"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name = "bedrock-vpc-endpoint"
  }
}

# Bedrock Runtime VPC 엔드포인트 (모델 실행용)
resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  
  private_dns_enabled = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = "*"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name = "bedrock-runtime-vpc-endpoint"
  }
}

# Bedrock Agent VPC 엔드포인트 (Agent용)
resource "aws_vpc_endpoint" "bedrock_agent_runtime" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock-agent-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  
  private_dns_enabled = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = "*"
        Action = [
          "bedrock:InvokeAgent"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name = "bedrock-agent-runtime-vpc-endpoint"
  }
}

# ElastiCache for Redis 설정
resource "aws_elasticache_subnet_group" "main" {
  name       = "bedrock-proxy-elasticache-subnet-group"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id          = "bedrock-proxy-redis"
  description                   = "Redis for Bedrock proxy rate limiting"
  node_type                     = "cache.t3.micro"
  num_cache_clusters            = 1
  port                          = 6379
  subnet_group_name             = aws_elasticache_subnet_group.main.name
  security_group_ids            = [aws_security_group.elasticache_sg.id]
  automatic_failover_enabled    = false
  multi_az_enabled              = false
  engine                        = "redis"
  engine_version                = "7.0"
  parameter_group_name          = "default.redis7"
  transit_encryption_enabled    = true
  at_rest_encryption_enabled    = true
}

# Lambda 프록시 함수 설정
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/lambda.zip"
  excludes    = ["layer"]
}

# Lambda Layer
data "archive_file" "lambda_layer_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda/layer"
  output_path = "${path.module}/lambda_layer.zip"
}

resource "aws_lambda_layer_version" "redis_layer" {
  layer_name          = "redis-layer"
  filename            = data.archive_file.lambda_layer_zip.output_path
  source_code_hash    = data.archive_file.lambda_layer_zip.output_base64sha256
  compatible_runtimes = ["python3.11"]
}

resource "aws_iam_role" "lambda_exec_role" {
  name = "bedrock-proxy-lambda-exec-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{
      Action    = "sts:AssumeRole",
      Effect    = "Allow",
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_policy" "lambda_policy" {
  name   = "BedrockProxyLambdaPolicy"
  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = [
          "bedrock:*"
        ],
        Resource = "*"
      },
      {
        Effect   = "Allow",
        Action   = [
            "ec2:CreateNetworkInterface",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DeleteNetworkInterface"
        ],
        Resource = "*"
      },
      {
        Effect = "Allow",
        Action = [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents"
        ],
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_attachment" {
  role       = aws_iam_role.lambda_exec_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

resource "aws_lambda_function" "bedrock_proxy" {
  function_name    = "bedrock-proxy-function"
  handler          = "proxy_function.lambda_handler"
  runtime          = "python3.11"
  role             = aws_iam_role.lambda_exec_role.arn
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 30
  layers           = [aws_lambda_layer_version.redis_layer.arn]

  vpc_config {
    subnet_ids         = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    security_group_ids = [aws_security_group.lambda_sg.id]
  }

  environment {
    variables = {
      REDIS_HOST      = aws_elasticache_replication_group.main.primary_endpoint_address
      REDIS_PORT      = aws_elasticache_replication_group.main.port
      RPM_LIMIT       = "100"
      TPM_LIMIT       = "10000"
    }
  }

  # VPC 엔드포인트가 생성된 후 Lambda 함수 생성
  depends_on = [
    aws_vpc_endpoint.bedrock,
    aws_vpc_endpoint.bedrock_runtime,
    aws_vpc_endpoint.bedrock_agent_runtime
  ]
}
