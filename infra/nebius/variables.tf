variable "run_tag" {
  description = "Exact run this configuration may create. Teardown targets only this tag."
  type        = string
  default     = "nebius-w11-20261015b"

  validation {
    condition     = var.run_tag == "nebius-w11-20261015b"
    error_message = "run_tag is locked to the clean D-0045 Nebius cell."
  }
}

variable "region" {
  description = "Project region. The owner moved this run to eu-south1 after us-central1 could not place a GPU. uk-south2 is not authorized."
  type        = string
  default     = "eu-south1"

  validation {
    condition     = var.region == "eu-south1"
    error_message = "region is locked to eu-south1."
  }
}

variable "platform" {
  description = "Nebius compute platform for one RTX PRO 6000 Server Edition in eu-south1."
  type        = string
  default     = "gpu-rtx6000-a"

  validation {
    condition     = var.platform == "gpu-rtx6000-a"
    error_message = "platform is locked to gpu-rtx6000-a."
  }
}

variable "preset" {
  description = "One GPU, 24 vCPU, 218 GiB. The 8-GPU preset is rejected."
  type        = string
  default     = "1gpu-24vcpu-218gb"

  validation {
    condition     = var.preset == "1gpu-24vcpu-218gb"
    error_message = "preset is locked to the one-GPU shape 1gpu-24vcpu-218gb."
  }
}

variable "project_id" {
  description = "Nebius project id. Supply it from the private tfvars file. Never commit it."
  type        = string

  validation {
    condition     = length(var.project_id) > 0 && !strcontains(var.project_id, "REPLACE")
    error_message = "project_id must be set in the private tfvars file."
  }
}

variable "network_id" {
  description = "Existing VPC network in the eu-south1 project. Supply it from the private tfvars file."
  type        = string

  validation {
    condition     = length(var.network_id) > 0 && !strcontains(var.network_id, "REPLACE")
    error_message = "network_id must be set in the private tfvars file."
  }
}

variable "subnet_id" {
  description = "Existing subnet that can allocate a dynamic public address for outbound downloads."
  type        = string

  validation {
    condition     = length(var.subnet_id) > 0 && !strcontains(var.subnet_id, "REPLACE")
    error_message = "subnet_id must be set in the private tfvars file."
  }
}

variable "ssh_public_key" {
  description = "One-line SSH public key for the bwlab user. A public key is not a credential file, but it stays in the private tfvars file."
  type        = string

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/=]+( .+)?$", var.ssh_public_key))
    error_message = "ssh_public_key must be one ssh-ed25519 or ssh-rsa public key line."
  }
}

variable "operator_cidr" {
  description = "Optional /32 allowed to SSH. Empty means no inbound SSH rule; use the Nebius serial console."
  type        = string
  default     = ""

  validation {
    condition     = var.operator_cidr == "" || can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}/32$", var.operator_cidr))
    error_message = "operator_cidr must be empty or a single IPv4 /32."
  }
}
