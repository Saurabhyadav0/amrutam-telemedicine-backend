# ---- Application Load Balancer (TLS 1.2+/1.3) --------------------------------------------

resource "aws_lb" "main" {
  #checkov:skip=CKV2_AWS_76:KnownBadInputsRuleSet (incl. Log4JRCE) is attached via aws_wafv2_web_acl.api's dynamic rule block, which Checkov can't evaluate
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = module.vpc.public_subnets
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = true
  idle_timeout               = 30

  access_logs {
    bucket  = aws_s3_bucket.logs.id
    prefix  = "alb"
    enabled = true
  }
}

resource "aws_lb_target_group" "api" {
  name                 = "${local.name}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = module.vpc.vpc_id
  deregistration_delay = 20 # matches uvicorn graceful shutdown
  health_check {
    path                = "/health/ready"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 10
    timeout             = 3
    matcher             = "200"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.domain_certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# Internal-only endpoints never leave the VPC.
resource "aws_lb_listener_rule" "block_internal" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"detail\":\"not found\"}"
      status_code  = "404"
    }
  }
  condition {
    path_pattern {
      values = ["/metrics", "/docs*", "/openapi.json", "/v1/payments/mock/*"]
    }
  }
}

# ---- WAF: managed OWASP rule sets + coarse per-IP rate limit -----------------------------

resource "aws_wafv2_web_acl" "api" {
  #checkov:skip=CKV_AWS_192:KnownBadInputsRuleSet (incl. Log4JRCE) is applied via the dynamic rule block
  name  = "${local.name}-api"
  scope = "REGIONAL"
  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = {
      AWSManagedRulesCommonRuleSet          = 10
      AWSManagedRulesKnownBadInputsRuleSet  = 20
      AWSManagedRulesSQLiRuleSet            = 30
      AWSManagedRulesAmazonIpReputationList = 40
    }
    content {
      name     = rule.key
      priority = rule.value
      override_action {
        none {}
      }
      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.key
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.key
        sampled_requests_enabled   = true
      }
    }
  }

  rule {
    name     = "per-ip-rate-limit"
    priority = 50
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = 3000 # per 5 minutes per IP; the app applies finer limits
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "per-ip-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-api"
    sampled_requests_enabled   = true
  }
}

# WAF log group names must start with "aws-waf-logs-".
resource "aws_cloudwatch_log_group" "waf" {
  name              = "aws-waf-logs-${local.name}"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.this["logs"].arn
}

resource "aws_wafv2_web_acl_logging_configuration" "api" {
  resource_arn            = aws_wafv2_web_acl.api.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]
  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
}

resource "aws_wafv2_web_acl_association" "api" {
  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.api.arn
}

# ---- Log bucket --------------------------------------------------------------------------

resource "aws_s3_bucket" "logs" {
  #checkov:skip=CKV_AWS_145:ALB access logs only support SSE-S3 (AES256), not KMS
  #checkov:skip=CKV_AWS_18:This is the log bucket; logging it to itself would loop
  #checkov:skip=CKV2_AWS_62:No consumers need object events for raw access logs
  #checkov:skip=CKV_AWS_144:Access logs are operational, not a system of record; no DR copy needed
  bucket        = "${local.name}-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ALB access logs only support SSE-S3; everything else in the account uses KMS.
resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    expiration {
      days = 400
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_elb_service_account" "main" {}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AlbAccessLogs"
        Effect    = "Allow"
        Principal = { AWS = data.aws_elb_service_account.main.arn }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.logs.arn}/alb/*"
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}
