variable "run_tag" {
  description = "Exact run this configuration may create. Teardown targets only this tag."
  type        = string
  default     = "aws-diag-20261014a"

  validation {
    condition     = var.run_tag == "aws-diag-20261014a"
    error_message = "run_tag is locked to the closed D-0041 session."
  }
}

variable "region" {
  description = "Region where the D-0041 preflight found a G7e offered and quota for one instance."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = var.region == "us-east-1"
    error_message = "region is locked to us-east-1 for the closed D-0041 session."
  }
}

variable "instance_type" {
  type    = string
  default = "g7e.8xlarge"

  validation {
    condition     = var.instance_type == "g7e.8xlarge"
    error_message = "instance_type is locked to the D-0041 capacity substitution g7e.8xlarge."
  }
}
