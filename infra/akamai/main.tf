# Exactly ONE single-GPU RTX PRO 6000 Blackwell instance for one benchmark
# session. No other resources are defined: no volumes, no load balancers, no
# firewall-managed fleets, nothing that could silently keep billing.
#
# Lifecycle rules (cost-guardrails.md; docs/decision-log.md D-0012):
# - plan is the default verb; apply and destroy each require a separate
#   explicit local owner approval phrase (blackwell-cloud wrapper);
# - every resource carries the project tag, the unique run tag, and a ttl
#   tag; teardown targets only ledger-recorded resources;
# - Akamai bills while the service EXISTS (powered off still bills): the
#   session ends by DELETING the instance after verified result export.

locals {
  project_tag = "blackwell-lab"
  # Tag set recorded in the run ledger and used by the read-only orphan
  # report. Tags are the only discovery mechanism; teardown still never
  # deletes by tag filter — only by the exact ledger-recorded resources.
  tags = [
    local.project_tag,
    "run:${var.run_tag}",
    "ttl-hours:${var.ttl_hours}",
    "phase:3",
  ]
}

resource "linode_instance" "gpu_baseline" {
  label  = "bwlab-${var.run_tag}"
  region = var.region
  type   = var.gpu_instance_type
  image  = var.instance_image
  tags   = local.tags

  # Public-key access only; no root password is set anywhere.
  authorized_keys = [var.authorized_ssh_key]

  # The bootstrap script is uploaded and run manually by the operator after
  # provisioning (infra/akamai/bootstrap/); no cloud-init user data is used,
  # keeping the applied configuration byte-auditable.

  lifecycle {
    # A GPU instance is never silently replaced: any change that would
    # destroy and recreate it must be an explicit destroy + apply, each with
    # its own approval.
    prevent_destroy = false
    ignore_changes  = []
  }
}
