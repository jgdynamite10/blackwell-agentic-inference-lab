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
| `check_akamai.py` | Akamai Cloud (Linode API) | Catalog listing works unauthenticated; account availability needs `LINODE_TOKEN` (read-only scope) |
| `check_gcp.py` | Google Cloud | Needs `gcloud` with a read-only identity |
| `check_aws.py` | AWS | Needs `aws` CLI with a read-only identity |

Usage (local machine):

```bash
python3 scripts/preflight/check_akamai.py --help
python3 scripts/preflight/check_akamai.py
python3 scripts/preflight/check_gcp.py
python3 scripts/preflight/check_aws.py
```

Each script prints a read-only banner, supports `--help`, and exits nonzero
naming the exact missing capability when it cannot complete its checks, so
gaps are documented rather than papered over. Mocked tests (no network, no
CLI) live in `tests/test_preflight.py`. Credential variable names are
documented in `.env.example`; never commit values or paste them into chat,
issues, PRs, or CI.
