# Input variables — safe validation, no embedded account data.
#
# Values come from an operator-local terraform.tfvars (excluded from Git) or
# -var flags. Nothing in this file or in version control may contain account
# identifiers, tokens, SSH private keys, or personal data.

variable "run_tag" {
  description = <<-EOT
    Unique lowercase tag for this exact run (e.g. "p3-pilot-20260907a").
    Applied to every resource; the teardown workflow targets ONLY resources
    recorded in this run's ledger.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{3,40}$", var.run_tag))
    error_message = "run_tag must be 4-41 chars: lowercase letters, digits, hyphens."
  }
}

variable "region" {
  description = <<-EOT
    Akamai region id for the instance (e.g. "us-ord"). Must be one of the
    documented RTX PRO 6000 limited-availability regions AND eligible for
    this account (verify with scripts/preflight/check_akamai.py first).
    EU, Singapore, and Jakarta regions carry documented price surcharges.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2,3}(-[a-z0-9]+)+$", var.region))
    error_message = "region must be a Linode region id such as us-ord."
  }
}

variable "gpu_instance_type" {
  description = <<-EOT
    The exact single-GPU RTX PRO 6000 Blackwell plan id, as shown in the
    onboarded account's catalog (the id is confirmed by the authenticated
    preflight; the plan is limited-availability and absent from the public
    catalog). This module deploys ONLY a single-GPU Blackwell plan: plans
    with more GPUs are rejected.
  EOT
  type        = string

  validation {
    # Single-GPU RTX PRO 6000 Blackwell plan ids only. Multi-GPU variants
    # (e.g. *-x2/-x4 suffixes) and non-Blackwell GPU plans are refused.
    condition = (
      can(regex("rtxpro6000|rtx-pro-6000", var.gpu_instance_type)) &&
      !can(regex("(x2|x4|x8)$", var.gpu_instance_type))
    )
    error_message = "gpu_instance_type must be a single-GPU RTX PRO 6000 Blackwell plan id."
  }
}

variable "instance_image" {
  description = "Pinned OS image (bootstrap assumes Ubuntu 24.04 LTS)."
  type        = string
  default     = "linode/ubuntu24.04"

  validation {
    condition     = can(regex("^linode/ubuntu24\\.04$", var.instance_image))
    error_message = "The bootstrap is validated against linode/ubuntu24.04 only."
  }
}

variable "authorized_ssh_key" {
  description = <<-EOT
    ONE SSH public key (never a private key) granting the operator access.
    Supplied locally via tfvars; never committed.
  EOT
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) ", var.authorized_ssh_key))
    error_message = "authorized_ssh_key must be an SSH PUBLIC key (ssh-ed25519 recommended)."
  }
}

variable "ttl_hours" {
  description = <<-EOT
    Intended maximum session length in hours, recorded as a ttl tag.
    Informational cost control: Akamai has no native auto-stop, and powering
    off does NOT stop billing — the instance must be DELETED to stop charges.
  EOT
  type        = number
  default     = 6

  validation {
    condition     = var.ttl_hours >= 1 && var.ttl_hours <= 24 && floor(var.ttl_hours) == var.ttl_hours
    error_message = "ttl_hours must be a whole number between 1 and 24."
  }
}
