# Read-only preflight scripts

Feasibility checks that are **safe by construction**: they only read public
catalogs or perform authenticated read-only queries, they never create, modify,
or delete cloud resources, and they never print credential values or account
identifiers. This complies with AGENTS.md sections 1–2 (Phase 1 access is
limited to read-only identity, availability, quota, instance-type,
regional-capacity, and pricing checks).

| Script | Provider | Needs credentials? |
| --- | --- | --- |
| `check_akamai.py` | Akamai Cloud (Linode API) | Public catalog works unauthenticated; account availability needs `LINODE_TOKEN` (read-only scope) |
| `check_gcp.py` | Google Cloud | Needs `gcloud` with a read-only identity |
| `check_aws.py` | AWS | Needs `aws` CLI with a read-only identity |

Usage:

```bash
python3 scripts/preflight/check_akamai.py
python3 scripts/preflight/check_gcp.py
python3 scripts/preflight/check_aws.py
```

Each script exits nonzero and names the exact missing capability when it
cannot complete its checks, so gaps are documented rather than papered over.
Credential variable names are documented in `.env.example`; never commit
values.
