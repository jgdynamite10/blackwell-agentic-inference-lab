# Pinned toolchain. State stays outside Git: pass -backend-config "path=..."
# pointing at a directory under the operator's home, never the repository.

terraform {
  required_version = "= 1.9.8"

  backend "local" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.72.1"
    }
  }
}

provider "aws" {
  region = var.region
}
