# Results Privacy

This repository is private, and genuine benchmark results are kept **outside
the Git working tree entirely** — they are never committed even to this
private repository, and nothing is released without the explicit approval
process in [publication-governance.md](publication-governance.md). This
document defines how that is enforced **by design**, not by discipline alone.

## Rules

1. Never write actual benchmark data inside the Git working tree. All genuine
   manifests, logs, traces, metrics, profiler outputs, model outputs, cost
   data, raw results, processed results, and reports go to the external
   directory identified by `LAB_RESULTS_DIR`.
2. Never commit: cloud account identifiers; project or tenant identifiers;
   internal hostnames or IP addresses; instance identifiers; private
   endpoints; signed URLs; customer or production data; credentials or
   tokens; Terraform state; private `.tfvars` files; environment files
   containing values; logs containing sensitive infrastructure metadata.
3. The repository **may** contain only: result schemas; synthetic example
   results (clearly labeled); test fixtures containing no real provider or
   account data; documentation of the result format; empty result-directory
   placeholders; redaction/sanitization scripts operating on sanitized input;
   methodology; documentation; code.

## Enforcement mechanisms

### 1. Private results location (`LAB_RESULTS_DIR`)

All benchmark tooling writes genuine results to a location **outside** the
repository, supplied through the `LAB_RESULTS_DIR` environment variable (see
`.env.example`).

The guard is implemented in `src/blackwell_lab/paths.py`
(`resolve_results_dir`) and is covered by tests. Expected behavior:

- **Real-run mode** (`RunMode.REAL`, the default): fails closed if
  `LAB_RESULTS_DIR` is unset or blank; **rejects every relative path before
  any resolution** (the location must be an unambiguous absolute path); and
  fails if the value — after resolving `~` and symlinks canonically — is the
  repository root or any directory beneath it. There is **no fallback** to
  `results/` or any other repository directory.
- **Synthetic/test mode** (`RunMode.SYNTHETIC`): must be requested explicitly
  by the caller; used only for synthetic examples and tests. An unset
  `LAB_RESULTS_DIR` yields "no persistence" (`None`) rather than a silent
  fallback, and any provided path is subject to the same relative-path and
  repository-interior rejections.
- The guard never creates a nonexistent directory merely while validating it;
  a nonexistent **external** absolute path is accepted (creation is the
  runner's explicit, logged action).

The benchmark runner (Phase 2 onward) must call the guard at startup, declare
its mode explicitly, and refuse to run when the guard raises. Runner logs must
never contain credentials, tokens, signed URLs, private endpoints, account
IDs, project IDs, tenant identifiers, or sensitive environment values.

### 2. `.gitignore` protections

`.gitignore` excludes `results/` (except `results/README.md`), `runs/`,
`artifacts/`, `outputs/`, raw data extensions, logs, profiling captures,
model weights, Terraform state, and env files. **`.gitignore` is a
defense-in-depth control only; it is not the privacy boundary** — the primary
control is that genuine results never exist inside the repository tree at all.

### 3. CI secret detection

CI runs the open-source Gitleaks CLI (pinned release, verified checksum,
redacted output, no findings artifact) on every push and pull request to catch
accidentally
committed credentials or tokens.

### 4. Review gate

All changes reach `main` via pull request with owner review (squash-only
merging). Release of genuine results additionally requires the dedicated
process in [publication-governance.md](publication-governance.md).

### 5. Sanitization flow (external, two-directory)

If a release is ever authorized, sanitization runs **locally** and entirely
outside the repository:

1. The sanitizer reads raw inputs **only from the external
   `LAB_RESULTS_DIR`**.
2. It writes the sanitized candidate to a **separate external staging
   directory** — never into the repository or `LAB_RESULTS_DIR` itself.
3. Raw data never enters the repository at any point.
4. The sanitized candidate may enter Git **only after explicit owner review
   and approval** of that specific candidate.
5. Raw provider bills and account-level cost records remain external
   permanently. The decision log may contain **only an explicitly approved
   sanitized summary** of costs — never raw billing information.

## If genuine results or secrets are ever committed

1. Treat the data as exposed the moment it is pushed; deleting the file in a
   follow-up commit is not sufficient.
2. Rotate any exposed credentials immediately.
3. Report per [../SECURITY.md](../SECURITY.md); the owner decides on history
   rewriting and disclosure.
4. Record the incident and remediation in [decision-log.md](decision-log.md).
