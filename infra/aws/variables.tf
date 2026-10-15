variable "run_tag" {
  description = "Exact run this configuration may create. Teardown targets only this tag."
  type        = string
  default     = "aws-w11-nvfp4-20261015a"

  validation {
    condition     = var.run_tag == "aws-w11-nvfp4-20261015a"
    error_message = "run_tag is locked to aws-w11-nvfp4-20261015a."
  }
}

variable "region" {
  description = "Region for the D-0047 NVFP4 cell. There is no other region."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = var.region == "us-east-1"
    error_message = "region is locked to us-east-1."
  }
}

variable "availability_zone" {
  description = "First us-east-1a. us-east-1c is the only capacity retry."
  type        = string
  default     = "us-east-1a"

  validation {
    condition     = contains(["us-east-1a", "us-east-1c"], var.availability_zone)
    error_message = "availability_zone must be us-east-1a or us-east-1c."
  }
}

variable "instance_type" {
  description = "On-demand 16-vCPU G7e only. Larger G7e sizes are refused."
  type        = string
  default     = "g7e.4xlarge"

  validation {
    condition     = var.instance_type == "g7e.4xlarge"
    error_message = "instance_type is locked to g7e.4xlarge."
  }
}
