# Secret containers only. Values are written out-of-band (scripts/generate_env.py output or a
# rotation Lambda), never through Terraform, so they never land in state or plan output.
locals {
  app_secrets = [
    "JWT_KEYS",
    "DATA_ENCRYPTION_KEYS",
    "BLIND_INDEX_KEY",
    "PAYMENT_WEBHOOK_SECRET",
    "PRESCRIPTION_SIGNING_KEY",
    "DATABASE_URL",
    "DATABASE_READ_URL",
    "REDIS_URL",
  ]
}

resource "aws_secretsmanager_secret" "app" {
  #checkov:skip=CKV2_AWS_57:Rotated by the documented key-ring procedure (new kid/version first, then retire), which needs app-aware ordering; DB credentials rotate via RDS-managed secrets
  for_each                = toset(local.app_secrets)
  name                    = "${local.name}/${each.key}"
  kms_key_id              = aws_kms_key.this["secrets"].arn
  recovery_window_in_days = 30
}
