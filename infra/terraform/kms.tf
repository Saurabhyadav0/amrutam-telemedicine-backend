# Separate customer-managed keys per data domain; AWS rotates key material yearly.
locals {
  kms_keys = {
    database = "RDS storage, snapshots and Performance Insights"
    cache    = "ElastiCache at-rest encryption"
    secrets  = "Secrets Manager (JWT keys, field-encryption keys, webhook/signing secrets)"
    logs     = "CloudWatch Logs and S3 log buckets"
    backup   = "AWS Backup vault"
  }
}

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "this" {
  for_each                = local.kms_keys
  description             = "${local.name} ${each.value}"
  enable_key_rotation     = true
  rotation_period_in_days = 365
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms[each.key].json
}

resource "aws_kms_alias" "this" {
  for_each      = local.kms_keys
  name          = "alias/${local.name}-${each.key}"
  target_key_id = aws_kms_key.this[each.key].key_id
}

data "aws_iam_policy_document" "kms" {
  #checkov:skip=CKV_AWS_109:In a key policy "*" means "this key"; access is delegated to IAM
  #checkov:skip=CKV_AWS_111:Same as above: key policies are scoped to their own key
  #checkov:skip=CKV_AWS_356:Same as above
  for_each = local.kms_keys

  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  dynamic "statement" {
    for_each = each.key == "logs" ? [1] : []
    content {
      sid       = "CloudWatchLogs"
      actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
      resources = ["*"]
      principals {
        type        = "Service"
        identifiers = ["logs.${var.region}.amazonaws.com"]
      }
    }
  }
}

# DR-region key for cross-region backup copies.
resource "aws_kms_key" "dr_database" {
  provider                = aws.dr
  description             = "${local.name} DR copies of RDS backups"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms["backup"].json
}
