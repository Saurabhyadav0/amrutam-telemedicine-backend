# ---- PostgreSQL (RDS, Multi-AZ) ----------------------------------------------------------

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "250" # ms: slow-query log feeds the latency SLO investigation
  }
  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "30000"
  }
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "primary" {
  identifier     = "${local.name}-pg"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_allocated_storage_gb * 5
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.this["database"].arn

  db_name                             = "amrutam"
  username                            = "amrutam_admin"
  manage_master_user_password         = true # generated + rotated in Secrets Manager
  master_user_secret_kms_key_id       = aws_kms_key.this["secrets"].arn
  iam_database_authentication_enabled = true

  multi_az               = true # synchronous standby in another AZ: automatic failover
  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.data.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.postgres.name

  backup_retention_period   = var.backup_retention_days # enables point-in-time recovery
  backup_window             = "20:00-21:00"             # 01:30-02:30 IST, lowest traffic
  maintenance_window        = "sun:21:30-sun:22:30"
  copy_tags_to_snapshot     = true
  delete_automated_backups  = false
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-pg-final"

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.this["database"].arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 30
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]
  auto_minor_version_upgrade            = true
}

# Read replicas serve search/listing/analytics (DATABASE_READ_URL).
resource "aws_db_instance" "replica" {
  #checkov:skip=CKV_AWS_157:Read scale-out only; the primary is Multi-AZ and replicas are recreated from it
  #checkov:skip=CKV_AWS_293:Replicas hold no unique data; deleting one never loses records
  count                      = var.db_read_replicas
  identifier                 = "${local.name}-pg-replica-${count.index}"
  replicate_source_db        = aws_db_instance.primary.identifier
  instance_class             = var.db_instance_class
  storage_encrypted          = true
  kms_key_id                 = aws_kms_key.this["database"].arn
  vpc_security_group_ids     = [aws_security_group.data.id]
  parameter_group_name       = aws_db_parameter_group.postgres.name
  publicly_accessible        = false
  skip_final_snapshot        = true
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.this["database"].arn
  monitoring_interval             = 30
  monitoring_role_arn             = aws_iam_role.rds_monitoring.arn
}

# Continuous copy of automated backups (snapshots + WAL) to the DR region: PITR there too.
resource "aws_db_instance_automated_backups_replication" "dr" {
  provider               = aws.dr
  source_db_instance_arn = aws_db_instance.primary.arn
  kms_key_id             = aws_kms_key.dr_database.arn
  retention_period       = 14
}

# ---- Redis (ElastiCache, Multi-AZ) -------------------------------------------------------

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = module.vpc.database_subnets
}

resource "random_password" "redis_auth" {
  length  = 48
  special = false
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.name}-redis"
  description                = "Rate limits, cache, arq queues"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.data.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.this["cache"].arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result

  snapshot_retention_limit = 3
  apply_immediately        = false
}

# ---- AWS Backup: immutable monthly copies (beyond RDS PITR) ------------------------------

resource "aws_backup_vault" "main" {
  name        = "${local.name}-vault"
  kms_key_arn = aws_kms_key.this["backup"].arn
}

# Compliance-mode lock: backups can't be deleted early, even by an admin (ransomware defence).
resource "aws_backup_vault_lock_configuration" "main" {
  backup_vault_name   = aws_backup_vault.main.name
  min_retention_days  = 30
  max_retention_days  = 2555 # ~7 years of medical records
  changeable_for_days = 3
}

resource "aws_backup_plan" "main" {
  name = "${local.name}-plan"

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(30 20 * * ? *)"
    lifecycle {
      delete_after = 35
    }
  }
  rule {
    rule_name         = "monthly-long-term"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 21 1 * ? *)"
    lifecycle {
      cold_storage_after = 30
      delete_after       = 2555
    }
  }
}

resource "aws_backup_selection" "db" {
  name         = "${local.name}-db"
  plan_id      = aws_backup_plan.main.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_db_instance.primary.arn]
}
