# Security Policy

## Scope

This is a **private** research repository, and it must remain private. Even
so, it is treated as if it could be exposed: it intentionally contains **no
credentials, no cloud account identifiers, no internal hostnames or IP
addresses, no instance identifiers, no Terraform state, and no genuine
benchmark results**. If you find any of these in the repository history,
please report it immediately (see below).

## Reporting a vulnerability or data exposure

- Use GitHub's private vulnerability reporting on this repository
  ("Security" tab → "Report a vulnerability"), or open a GitHub issue that
  describes only the *category* of the problem (for example, "possible
  committed secret in file X") without quoting the sensitive value.
- Do not post secret values, account identifiers, or infrastructure metadata
  in issues or pull requests, even though the repository is private.

## Secret handling rules

1. Credentials are provided to tooling only through environment variables or
   provider-standard credential files stored outside this repository.
2. `.env.example` documents variable **names** only. Never commit a `.env`
   file containing values.
3. `.gitignore` excludes common secret and state file patterns, but it is a
   convenience, not a security boundary. Treat any secret that reaches a
   commit — even a deleted one — as compromised: rotate it immediately.
4. CI runs secret detection (gitleaks) on every push and pull request.

## Cloud accounts

Feasibility and benchmark tooling must use scoped, least-privilege identities.
Read-only identities are required for all Phase 1 work. See
[AGENTS.md](AGENTS.md) for the full safety rules.

## Supported versions

This project has no released versions yet. Security fixes are applied to
`main`.
