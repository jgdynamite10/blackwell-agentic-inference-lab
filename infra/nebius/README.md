# Nebius W11 replication kit (D-0045)

One on-demand RTX PRO 6000 Blackwell Server Edition in `us-central1`.
The purchasable shape is platform `gpu-rtx6000`, preset
`1gpu-24vcpu-218gb`: 1 GPU, 24 vCPU, 218 GiB RAM. The published GPU
price checked on 2026-10-15 is **$1.80 per GPU-hour**. The 400 GiB
network SSD is billed separately. This kit does not create an instance
by itself.

The image family is `ubuntu24.04-cuda13.0` (Ubuntu 24.04, CUDA 13.0,
NVIDIA driver 580.x). Bootstrap still requires the lab pin
`nvidia-driver-580-server-open=580.178.04-0ubuntu0.24.04.1`. The image
docs do not promise that exact package. A mismatch stops the cell; the
pin is not relaxed here.

This host is larger than the finished comparison hosts (16 vCPU, 125 GiB
on AWS and 173 GiB on Akamai). The CPU is Intel Xeon 6776P. Record those
differences with the result. There is no second region: `gpu-rtx6000-a`
in `uk-south2` and `eu-south1` is not authorized.

## What stays out of Git

Copy `terraform.tfvars.example` to
`~/.local/share/blackwell-lab/nebius-w11-20261015a/terraform.tfvars`
and fill the project, network, and subnet identifiers there. Terraform
state and plans stay in that same private directory. Genuine results
stay in `LAB_RESULTS_DIR`, outside this repository.

## Access

The security group allows outbound traffic and denies everything else.
Set `operator_cidr` to your current IPv4 `/32` when you want SSH as
user `bwlab`. Leave it empty to keep SSH closed and use the Nebius
serial console. The public address is dynamic and is removed with the
instance. Serving stays on `127.0.0.1:8000`.

## Billing

`recovery_policy` is `FAIL`. The 10-hour first-boot timer shuts the
guest down, and Nebius does not restart it, so compute billing stops.
The boot disk keeps billing until the instance is deleted. Export the
results, then run the operator `destroy` command. Do not stop at
shutdown.

## Commands

Authenticate locally. Do not paste the token into chat, a pull request,
or a file in this repository.

```bash
export NEBIUS_IAM_TOKEN="$(nebius iam get-access-token)"
python3 scripts/nebius_w11_operator.py plan
```

Apply and destroy stay refused until the process environment contains
the exact phrase recorded in `scripts/nebius_w11_operator.py`.

On the host, after copying a mode-0600 token file to
`/var/lib/bwlab/hf-token`:

```bash
export BWLAB_GIT_REF=d0045-nebius-w11-kit
sudo --preserve-env=BWLAB_GIT_REF bash scripts/nebius_w11_full_remote.sh prepare
sudo bash scripts/nebius_w11_full_remote.sh bootstrap
sudo bash scripts/nebius_w11_full_remote.sh fetch
sudo bash scripts/nebius_w11_full_remote.sh serve
sudo bash scripts/nebius_w11_full_remote.sh cell
sudo bash scripts/nebius_w11_full_remote.sh pack
```

Copy `/var/lib/bwlab/results-export.tar` off the host into
`LAB_RESULTS_DIR` before destroy. `fetch` deletes the token file after
reading it.
