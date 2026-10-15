output "run_tag" {
  value = var.run_tag
}

output "region" {
  value = var.region
}

output "platform" {
  value = var.platform
}

output "preset" {
  value = var.preset
}

output "instance_name" {
  value = nebius_compute_v1_instance.gpu.name
}
