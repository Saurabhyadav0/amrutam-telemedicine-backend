locals {
  name = "amrutam-${var.environment}"
  # Explicit AZs: the subnet layout must not shift if AWS adds a zone to the region.
  azs = var.availability_zones
}

# Three tiers across three AZs: public (ALB, NAT), private (ECS tasks), database (RDS, Redis).
module "vpc" {
  #checkov:skip=CKV_TF_1:Registry module pinned to an exact version; the lock file pins provider hashes
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3"

  name             = local.name
  cidr             = var.vpc_cidr
  azs              = local.azs
  public_subnets   = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, i)]
  private_subnets  = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, i + 10)]
  database_subnets = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, i + 20)]

  create_database_subnet_group       = true
  create_database_subnet_route_table = true # no route to the internet from data subnets
  enable_nat_gateway                 = true
  one_nat_gateway_per_az             = var.environment == "production"
  enable_dns_hostnames               = true

  enable_flow_log                      = true
  create_flow_log_cloudwatch_log_group = true
  create_flow_log_cloudwatch_iam_role  = true
  flow_log_max_aggregation_interval    = 60
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public HTTPS entry"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  #checkov:skip=CKV_AWS_260:Port 80 only answers with a 301 to HTTPS (see aws_lb_listener.http_redirect)
  ingress {
    description = "HTTP (redirected to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description     = "To API tasks only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "API and worker tasks"
  vpc_id      = module.vpc.vpc_id

  egress {
    description = "Postgres inside the VPC"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    description = "Redis inside the VPC"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    description = "HTTPS only: AWS APIs (ECR, Secrets Manager, KMS, logs) and the payment gateway via NAT"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "app_from_alb" {
  type                     = "ingress"
  description              = "ALB to API"
  security_group_id        = aws_security_group.app.id
  source_security_group_id = aws_security_group.alb.id
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
}

resource "aws_security_group" "data" {
  name        = "${local.name}-data"
  description = "RDS and ElastiCache: reachable only from app tasks"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "Postgres from app"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  ingress {
    description     = "Redis from app"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}
