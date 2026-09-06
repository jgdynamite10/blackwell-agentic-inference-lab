# Read-only preflight scripts

Feasibility checks that are **safe by construction**: they only read
catalogs or perform authenticated read-only queries, they cannot provision,
update, resize, stop, start, terminate, or delete cloud resources, and they
never print credential values or account identifiers. This complies with
AGENTS.md sections 1, 3, and 4.

**Execution boundary:** these scripts are intended to be run **locally by the
operator** in their authenticated environment. The hosted Cloud Agent must not
run credential-dependent preflight checks and never receives provider
credentials (AGENTS.md, section 3).

| Script | Provider | Needs credentials? |
| --- | --- | --- |
| `check_akamai.py` | Akamai Cloud (Linode API) | `--public-only` catalog mode works unauthenticated; authenticated readiness requires `LINODE_TOKEN` (read-only scope) **and `--region`** for account-level deployability and price decisions |
| `check_gcp.py` | Google Cloud | Needs `gcloud` with a read-only identity |
| `check_aws.py` | AWS | Needs `aws` CLI with a read-only identity |

Usage (local machine):

```bash
python3 scripts/preflight/check_akamai.py --help
python3 scripts/preflight/check_akamai.py --public-only   # catalog only, no token needed
python3 scripts/preflight/check_akamai.py --region us-sea   # full check, requires LINODE_TOKEN
python3 scripts/preflight/check_gcp.py
python3 scripts/preflight/check_aws.py
```

Behavioral guarantees:

- Each script prints a read-only banner and supports `--help`.
- Exit code 0 means **every requested check completed**. A missing CLI,
  missing credentials, or a blocked/failed account-level check exits nonzero
  naming the missing capability — a missing `LINODE_TOKEN` is only a success
  when the operator explicitly requested `--public-only`.
- On failure the scripts print **sanitized, generic, actionable messages
  only** — never raw CLI stdout/stderr or exception text, which can embed
  account IDs, project IDs, ARNs, tokens, credential paths, or private
  endpoints. Mocked tests inject synthetic values of each of those kinds into
  simulated errors and assert none appear in output.
- Instance-type/machine-type *offering* checks report that a type can be
  requested in a region or zone; they do **not** prove immediate/live
  capacity. The AWS script does not query the AWS Pricing API and makes no
  pricing claims.

Mocked tests (no network, no CLI) live in `tests/test_preflight.py`.
Credential variable names are documented in `.env.example`. Provider
credentials stay in the owner's local credential store or a temporary
process environment — never in a repository-local `.env` file, even if
gitignored. Never paste values into chat, issues, PRs, or CI.
