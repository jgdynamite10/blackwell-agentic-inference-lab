# Contributing

This is a public research repository licensed under the
[Apache License 2.0](LICENSE). External issues and pull requests are welcome,
subject to maintainer review; acceptance remains at the maintainer's
discretion.

It is also a research project with strict safety and integrity rules; read
[AGENTS.md](AGENTS.md) before making any change.

## Contribution terms

By intentionally submitting a contribution to this repository, you:

1. represent that you have the right to submit it;
2. agree that the contribution is provided under the Apache License 2.0
   (consistent with Section 5 of the license, and with no additional terms);
3. must not submit credentials, private provider information, customer data,
   employer-confidential material, or genuine benchmark results;
4. must use synthetic fixtures in tests;
5. must follow [AGENTS.md](AGENTS.md) and the measurement and governance
   policies in `methodology/` and `docs/`.

## Ground rules

1. **Never commit secrets, cloud account identifiers, infrastructure
   metadata, Terraform state, or genuine benchmark results.** See
   [SECURITY.md](SECURITY.md) and [docs/results-privacy.md](docs/results-privacy.md).
2. Only synthetic data may appear in this repository. Example files must be
   clearly labeled as synthetic.
3. Do not change the experimental methodology (anything under `methodology/`
   or `schemas/`) without an entry in [docs/decision-log.md](docs/decision-log.md)
   explaining the change and its motivation.
4. Work happens on branches and is merged through pull requests. Do not push
   to `main`. The repository uses squash-only merging.
5. Respect the phase roadmap in [docs/roadmap.md](docs/roadmap.md). Do not
   open pull requests implementing unauthorized phases.
6. Do not put personal email addresses in commit metadata, commit messages,
   co-author trailers, documentation, fixtures, or PR text. Owner-attributed
   commits use the owner's GitHub no-reply address
   (`1800971+jgdynamite@users.noreply.github.com`).

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Before opening a pull request

```bash
ruff check .
ruff format --check .
pytest
```

CI runs the same checks plus secret detection. All checks must pass.

## Style

- Python code is formatted with `ruff format` and linted with `ruff check`
  (configuration in `pyproject.toml`).
- Documentation is plain Markdown. Prefer complete sentences and cite official
  sources for factual claims about products, pricing, or compatibility.
- Distinguish verified facts from assumptions in all feasibility or research
  documents.

## Commit messages

Use short, descriptive, imperative-mood messages ("Add run-manifest schema",
not "Added stuff"). One logical change per commit where practical.
