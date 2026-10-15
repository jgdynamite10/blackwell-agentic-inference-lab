# D-0045 Nebius replication kit. State stays outside Git: the operator
# script passes -backend-config "path=..." under the operator's home.
# Authentication is NEBIUS_IAM_TOKEN in the process environment. Never
# put a token, project id, or key in this directory.

terraform {
  required_version = "= 1.9.8"

  backend "local" {}

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = "0.6.8"
    }
  }
}

provider "nebius" {}
