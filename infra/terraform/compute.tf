# ---- Container registry ------------------------------------------------------------------

resource "aws_ecr_repository" "api" {
  name                 = "amrutam-api"
  image_tag_mutability = "IMMUTABLE" # a tag (git SHA) always means the same bits
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 50 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 50 }
      action       = { type = "expire" }
    }]
  })
}

# ---- ECS Fargate -------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each          = toset(["api", "worker", "migrate"])
  name              = "/amrutam/${var.environment}/${each.key}"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.this["logs"].arn
}

locals {
  image = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"

  common_env = [
    { name = "ENVIRONMENT", value = var.environment },
    { name = "LOG_LEVEL", value = "INFO" },
    { name = "JWT_ACTIVE_KID", value = "k1" },
    { name = "DATA_ACTIVE_KEY_VERSION", value = "v1" },
    { name = "DB_POOL_SIZE", value = "15" },
    { name = "DB_MAX_OVERFLOW", value = "0" },
    { name = "OTEL_TRACES_SAMPLER", value = "parentbased_traceidratio" },
    { name = "OTEL_TRACES_SAMPLER_ARG", value = "0.1" },
    { name = "FORWARDED_ALLOW_IPS", value = var.vpc_cidr }, # trust X-Forwarded-For from the ALB only
  ]
  common_secrets = [
    for name in local.app_secrets : { name = name, valueFrom = aws_secretsmanager_secret.app[name].arn }
  ]

  container = {
    for svc, cmd in {
      api     = ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "20"]
      worker  = ["arq", "app.worker.main.WorkerSettings"]
      migrate = ["alembic", "upgrade", "head"]
      } : svc => {
      name                   = svc
      image                  = local.image
      command                = cmd
      essential              = true
      readonlyRootFilesystem = true
      user                   = "10001"
      environment            = local.common_env
      secrets                = local.common_secrets
      portMappings           = svc == "api" ? [{ containerPort = 8000, protocol = "tcp" }] : []
      linuxParameters        = { initProcessEnabled = true }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app[svc].name
          awslogs-region        = var.region
          awslogs-stream-prefix = svc
        }
      }
    }
  }
}

resource "aws_ecs_task_definition" "app" {
  for_each                 = { api = [1024, 2048], worker = [512, 1024], migrate = [512, 1024] }
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value[0]
  memory                   = each.value[1]
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode([local.container[each.key]])
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64" # Graviton: ~20% cheaper per vCPU
  }
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.app["api"].arn
  desired_count                     = var.api_desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30
  enable_execute_command            = false

  network_configuration {
    subnets          = module.vpc.private_subnets
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  deployment_minimum_healthy_percent = 100 # rolling deploys never drop capacity
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true # failing health checks roll the deploy back automatically
  }
  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling
  }
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app["worker"].arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = module.vpc.private_subnets
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

# ---- Autoscaling: CPU and per-target request rate ----------------------------------------

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api_desired_count
  max_capacity       = var.api_max_count
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value = 55 # async Python latency degrades well before 100% CPU
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_policy" "api_requests" {
  name               = "${local.name}-api-requests"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value = 60 # requests/s per task; re-calibrate from staging load tests
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.main.arn_suffix}/${aws_lb_target_group.api.arn_suffix}"
    }
  }
}
