output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "db_endpoint" {
  value = aws_db_instance.primary.address
}

output "db_replica_endpoints" {
  value = aws_db_instance.replica[*].address
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "migrate_task_definition" {
  description = "Run as a one-off ECS task before each deploy (see .github/workflows/deploy.yml)"
  value       = aws_ecs_task_definition.app["migrate"].arn
}
