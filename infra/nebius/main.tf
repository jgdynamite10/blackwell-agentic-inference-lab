# D-0045 kit for one on-demand Nebius GPU. Do not run terraform apply
# by hand. scripts/nebius_w11_operator.py is the approval gate.
#
# The managed boot disk is deleted with the instance. A guest shutdown
# uses recovery_policy FAIL so Nebius does not restart the VM and keep
# charging compute. Disk charges continue until the instance is deleted.

locals {
  labels = {
    project = "blackwell-lab"
    run     = var.run_tag
    ttl     = "10h-from-first-boot"
    phase   = "d-0045"
    region  = var.region
  }
}

resource "nebius_vpc_v1_security_group" "host" {
  parent_id  = var.project_id
  name       = "bwlab-${var.run_tag}"
  network_id = var.network_id
  labels     = local.labels
}

resource "nebius_vpc_v1_security_rule" "egress" {
  parent_id = nebius_vpc_v1_security_group.host.id
  name      = "egress-downloads"
  access    = "ALLOW"
  protocol  = "ANY"
  priority  = 200
  labels    = local.labels

  egress = {
    destination_cidrs = ["0.0.0.0/0"]
  }
}

resource "nebius_vpc_v1_security_rule" "ssh" {
  count = var.operator_cidr == "" ? 0 : 1

  parent_id = nebius_vpc_v1_security_group.host.id
  name      = "ssh-operator"
  access    = "ALLOW"
  protocol  = "TCP"
  priority  = 100
  labels    = local.labels

  ingress = {
    source_cidrs      = [var.operator_cidr]
    destination_ports = [22]
  }
}

resource "nebius_compute_v1_instance" "gpu" {
  parent_id = var.project_id
  name      = "bwlab-${var.run_tag}"
  labels    = local.labels

  resources = {
    platform = var.platform
    preset   = var.preset
  }

  # Omitting preemptible selects an on-demand VM. Spot is not authorized.
  recovery_policy = "FAIL"
  stopped         = false

  cloud_init_user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    ssh_public_key = var.ssh_public_key
  })

  boot_disk = {
    attach_mode = "READ_WRITE"
    device_id   = "bwlab-boot"
    managed_disk = {
      name   = "bwlab-${var.run_tag}-boot"
      labels = local.labels
      spec = {
        type            = "NETWORK_SSD"
        size_gibibytes  = 400
        forbid_deletion = false
        source_image_family = {
          parent_id    = var.project_id
          image_family = "ubuntu24.04-cuda13.0"
        }
      }
    }
  }

  network_interfaces = [
    {
      name       = "eth0"
      subnet_id  = var.subnet_id
      ip_address = {}
      public_ip_address = {
        static = false
      }
      security_groups = [
        { id = nebius_vpc_v1_security_group.host.id }
      ]
    }
  ]

  lifecycle {
    precondition {
      condition     = nebius_vpc_v1_security_rule.egress.access == "ALLOW"
      error_message = "The egress rule must exist before the instance is planned."
    }
  }
}
