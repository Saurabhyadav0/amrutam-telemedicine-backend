variable "environment" {
  type        = string
  description = "staging | production"
}

variable "region" {
  type    = string
  default = "ap-south-1" # Mumbai
}

variable "dr_region" {
  type    = string
  default = "ap-south-2" # Hyderabad
}

variable "availability_zones" {
  type    = list(string)
  default = ["ap-south-1a", "ap-south-1b", "ap-south-1c"]
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "domain_certificate_arn" {
  type        = string
  description = "ACM certificate for the public API hostname"
}

variable "image_tag" {
  type        = string
  description = "Immutable image tag (git SHA) deployed to ECS"
}

variable "api_desired_count" {
  type    = number
  default = 4
}

variable "api_max_count" {
  type    = number
  default = 20
}

variable "worker_desired_count" {
  type    = number
  default = 2
}

variable "db_instance_class" {
  type    = string
  default = "db.r6g.xlarge"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 200
}

variable "db_read_replicas" {
  type    = number
  default = 1
}

variable "redis_node_type" {
  type    = string
  default = "cache.r6g.large"
}

variable "backup_retention_days" {
  type    = number
  default = 30
}

variable "alarm_email" {
  type    = string
  default = ""
}
