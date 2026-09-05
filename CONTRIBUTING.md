# Contributing

Thank you for your interest. This is a research repository with strict safety
and integrity rules; please read [AGENTS.md](AGENTS.md) before contributing.

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
