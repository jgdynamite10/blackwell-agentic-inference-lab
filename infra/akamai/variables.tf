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
    Akamai region id for the authorized D-0014 diagnostic pilot. Locked to
    us-sea. A saved Terraform plan verifies this intended configuration only;
    it does not prove live capacity.
  EOT
  type        = string
  default     = "us-sea"

  validation {
    condition     = var.region == "us-sea"
    error_message = "region must equal us-sea for the authorized D-0014 diagnostic pilot."
  }
}

variable "gpu_instance_type" {
  description = <<-EOT
    Dedicated single-GPU RTX PRO 6000 Blackwell plan for the authorized
    D-0014 diagnostic pilot. Locked to g3-gpu-rtxpro6000-blackwell-1.
  EOT
  type        = string
  default     = "g3-gpu-rtxpro6000-blackwell-1"

  validation {
    condition     = var.gpu_instance_type == "g3-gpu-rtxpro6000-blackwell-1"
    error_message = "gpu_instance_type must equal g3-gpu-rtxpro6000-blackwell-1 for the authorized D-0014 diagnostic pilot."
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

variable "management_cidr" {
  description = <<-EOT
    Owner-supplied IPv4 management CIDR. SSH (TCP/22) is permitted ONLY from
    this range by the run's Cloud Firewall; every other inbound flow is
    dropped, and the model-serving port is never exposed publicly (serving
    binds to loopback on the instance). 0.0.0.0/0, ::/0, and any /0 prefix
    are refused: a public instance protected only by an SSH key is not an
    acceptable posture for this project.
  EOT
  type        = string

  validation {
    condition = (
      can(cidrhost(var.management_cidr, 0)) &&
      can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/([1-9]|[12][0-9]|3[0-2])$", var.management_cidr))
    )
    error_message = "management_cidr must be a specific IPv4 CIDR with prefix /1-/32; 0.0.0.0/0 and ::/0 are refused."
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
    Locked to 6 for the authorized D-0014 diagnostic pilot and the D-0017
    Akamai minimum valuable lab. Informational cost control: Akamai has no
    native auto-stop, and powering off does NOT stop billing — the instance
    must be DELETED to stop charges.
  EOT
  type        = number
  default     = 6

  validation {
    condition     = var.ttl_hours == 6
    error_message = "ttl_hours must equal 6 for the authorized six-hour Akamai session envelope."
  }
}
