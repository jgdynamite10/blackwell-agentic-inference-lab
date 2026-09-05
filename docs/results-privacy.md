# Results Privacy

The repository is public; genuine benchmark results are private until
explicitly approved for publication through the process in
[publication-governance.md](publication-governance.md). This document defines
how privacy is enforced **by design**, not by discipline alone.

## Rules

1. Never commit genuine raw, normalized, intermediate, or unpublished
   benchmark results to this repository.
2. Never commit: cloud account identifiers; internal hostnames or IP
   addresses; instance identifiers; customer or production data; credentials
   or tokens; Terraform state; private `.tfvars` files; environment files
   containing values; logs containing sensitive infrastructure metadata.
3. The public repository **may** contain: result schemas; synthetic example
   results (clearly labeled); documentation of the result format; empty
   result-directory placeholders; publication scripts operating on sanitized
   input.

## Enforcement mechanisms

### 1. Private results location (`LAB_RESULTS_DIR`)

All benchmark tooling writes genuine results to a location **outside** the
public repository, supplied through the `LAB_RESULTS_DIR` environment variable
(see `.env.example`).

The guard is implemented in `src/blackwell_lab/paths.py`
(`resolve_results_dir`) and is covered by tests. The benchmark runner (Phase 2
onward) must call it at startup and **refuse to run** if the configured
private-results location is unset or resolves inside the repository — including
through symlinks or relative paths.

### 2. `.gitignore` protections

`.gitignore` excludes `results/` (except `results/README.md`), `runs/`,
`artifacts/`, `outputs/`, raw data extensions, logs, profiling captures,
model weights, Terraform state, and env files. **`.gitignore` is a
convenience, not a security boundary** — the primary control is that genuine
results never exist inside the repository tree at all.

### 3. CI secret detection

CI runs gitleaks on every push and pull request to catch accidentally
committed credentials or tokens.

### 4. Review gate

All changes reach `main` via pull request with owner review (squash-only
merging). Release of genuine results additionally requires the dedicated
process in [publication-governance.md](publication-governance.md).

## If genuine results or secrets are ever committed

1. Treat the data as exposed the moment it is pushed; deleting the file in a
   follow-up commit is not sufficient.
2. Rotate any exposed credentials immediately.
3. Report per [../SECURITY.md](../SECURITY.md); the owner decides on history
   rewriting and disclosure.
4. Record the incident and remediation in [decision-log.md](decision-log.md).
