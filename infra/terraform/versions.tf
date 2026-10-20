terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.69"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # State in S3 with locking. Configure per environment:
  #   terraform init -backend-config=envs/prod.backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project     = "amrutam-telemed"
      environment = var.environment
      managed_by  = "terraform"
      data_class  = "restricted-phi"
    }
  }
}

# Disaster-recovery region. Hyderabad keeps backups inside India (DPDP data localisation).
provider "aws" {
  alias  = "dr"
  region = var.dr_region
}
