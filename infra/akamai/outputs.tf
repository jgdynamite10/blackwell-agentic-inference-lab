# Outputs feed the run ledger (stored outside Git). The IP address is marked
# sensitive so casual terminal output does not capture infrastructure
# addressing; the operator retrieves it deliberately when connecting.

output "instance_id" {
  description = "Provider id of the created instance (ledger key for exact teardown)."
  value       = linode_instance.gpu_baseline.id
}

output "instance_label" {
  description = "Instance label (bwlab-<run_tag>)."
  value       = linode_instance.gpu_baseline.label
}

output "instance_region" {
  description = "Region the instance was created in."
  value       = linode_instance.gpu_baseline.region
}

output "instance_type" {
  description = "Purchasable plan id (never an instance identifier)."
  value       = linode_instance.gpu_baseline.type
}

output "instance_tags" {
  description = "Tags applied (project, run, ttl, phase)."
  value       = linode_instance.gpu_baseline.tags
}

output "instance_ipv4" {
  description = "Instance IPv4 addresses (sensitive: retrieved deliberately, never logged)."
  value       = linode_instance.gpu_baseline.ipv4
  sensitive   = true
}
