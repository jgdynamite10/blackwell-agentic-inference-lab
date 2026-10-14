output "run_tag" {
  value = var.run_tag
}

output "instance_type" {
  value = var.instance_type
}

output "region" {
  value = var.region
}

output "ami_id" {
  value = data.aws_ami.ubuntu.id
}
