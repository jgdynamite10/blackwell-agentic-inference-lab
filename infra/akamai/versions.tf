# Pinned toolchain: reproducible plans require exact provider versions.
# Version bumps are deliberate, reviewed changes (decision-log entry when the
# change affects a frozen baseline).

terraform {
  # Terraform CLI: any 1.9+ release of the 1.x series.
  required_version = ">= 1.9.0, < 2.0.0"

  # State lives OUTSIDE the Git working tree: the lifecycle wrapper
  # (blackwell-cloud init/plan/apply) configures this local backend with an
  # external state path under LAB_RESULTS_DIR and an external TF_DATA_DIR.
  # Running terraform manually without that configuration is unsupported.
  backend "local" {}

  required_providers {
    linode = {
      source = "linode/linode"
      # Exact pin (released 2026-07-10). The provider reads LINODE_TOKEN from
      # the environment; no credential ever appears in this configuration.
      version = "4.1.0"
    }
  }
}

provider "linode" {
  # Authentication comes exclusively from the LINODE_TOKEN environment
  # variable in the owner's local environment. Never write a token into any
  # .tf or .tfvars file.
}
