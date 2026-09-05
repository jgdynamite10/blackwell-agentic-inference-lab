# AGENTS.md — Canonical Project Rules

This file is the canonical instruction file for every human and AI agent
working in this repository. These rules are permanent unless the project owner
explicitly revises them in writing.

## 1. Cloud-resource safety

1. **Never provision, resize, modify, or delete cloud resources without the
   project owner's explicit approval.** This applies to every provider and
   every resource type: VMs, GPUs, clusters, disks, buckets, networks, DNS,
   IAM entities, and anything else that costs money or changes account state.
2. **Never run `terraform apply` or `terraform destroy` without the project
   owner's explicit approval.** `terraform plan` against approved
   configurations is permitted for review purposes only.
3. Use scoped identities and read-only operations during feasibility checks.
4. During Phase 1, limit Akamai Cloud, Google Cloud, and AWS access to
   read-only identity, availability, quota, instance-type, regional-capacity,
   and pricing checks.
5. Do not create, modify, resize, start, stop, or delete AWS resources until
   the dedicated AWS replication phase (Phase 6) is explicitly authorized.
6. Follow the spend controls in [docs/cost-guardrails.md](docs/cost-guardrails.md).

## 2. Credential handling

1. **Never print, save, commit, or expose credentials.** This includes API
   keys, tokens, service-account files, SSH keys, kubeconfigs, and `.tfvars`
   files containing values.
2. Never request that credentials be committed to the repository, or add them
   to source files, GitHub issues, or pull-request text.
3. Credentials enter the environment only through environment variables or
   provider-standard credential files outside the repository. `.env.example`
   documents variable names only, never values.
4. Treat `.gitignore` as a convenience, not a security boundary.

## 3. Data and results integrity

1. **Use only synthetic workload data.** The benchmark must never connect to
   production Akamai systems or use customer information.
2. **Treat genuine raw benchmark results as private and immutable.** Never
   commit genuine raw, normalized, intermediate, or unpublished results to
   this public repository. See [docs/results-privacy.md](docs/results-privacy.md).
3. Genuine results are written only to the private location configured via
   `LAB_RESULTS_DIR`, which must resolve outside this repository. The runner
   must refuse to start otherwise.
4. Publication of genuine results happens only through the process in
   [docs/publication-governance.md](docs/publication-governance.md).

## 4. Research integrity

1. **Record a manifest for every experiment.** Every run manifest must
   identify the Git commit, model artifact and hash, container digest,
   serving-engine version, driver, CUDA version, operating system, hardware,
   cloud region, generation settings, workload, concurrency, and timing
   conditions. The schema is [schemas/run-manifest.schema.json](schemas/run-manifest.schema.json).
2. **Do not revise the experimental methodology after examining results
   without documenting the revision** in [docs/decision-log.md](docs/decision-log.md),
   including what changed, why, and which results predate the change.
3. **Do not make provider-superiority claims that exceed the evidence.**
4. **Record environmental differences rather than hiding them.** CPU
   architecture, vCPU count, memory, storage, networking, virtualization,
   drivers, and regional capacity may differ across providers and must be
   captured in run manifests and reports.
5. Keep controlled-resource results and provider-native results strictly
   separated in analysis and reporting (see
   [methodology/experiment-matrix.md](methodology/experiment-matrix.md)).

## 5. Phase discipline

1. **Stop at the end of the currently authorized phase.** The roadmap is in
   [docs/roadmap.md](docs/roadmap.md). Work on a later phase begins only after
   the project owner explicitly authorizes it.
2. Currently authorized: **Phase 1 only.**

## 6. Repository hygiene

1. Never commit: cloud account identifiers, internal hostnames or IP
   addresses, instance identifiers, customer or production data, credentials
   or tokens, Terraform state, private `.tfvars` files, environment files
   containing values, or logs containing sensitive infrastructure metadata.
2. Run tests and linting before submitting changes: `ruff check .`,
   `ruff format --check .`, `pytest`.
3. Do not push directly to `main`. All changes go through pull requests. The
   repository is configured for squash-only merging, and the project owner
   merges.
